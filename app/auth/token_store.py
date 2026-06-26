from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StoredAccount:
    """Metadata about a stored Gmail account token (never exposes the token)."""

    email: str
    path: Path
    valid: bool
    expired: bool
    has_refresh_token: bool
    scopes: list[str]
    last_refresh: str | None  # ISO timestamp of the last successful refresh/save


class TokenStore:
    """Manages OAuth tokens under ``storage/tokens/`` keyed by account email.

    Layout (multi-account ready; only one account is "active" today):

        storage/tokens/
         ├─ active.json              # {"active": "john@gmail.com"}
         ├─ john@gmail.com.json      # google authorized-user token
         └─ company@gmail.com.json

    Tokens never leave the server. Only non-sensitive metadata (email,
    validity, last refresh) is surfaced to callers. A legacy single-file
    ``token.json`` is migrated transparently on first read.
    """

    def __init__(self, tokens_dir: Path, *, legacy_token_path: Path | None = None) -> None:
        self.tokens_dir = tokens_dir.expanduser().resolve()
        try:
            self.tokens_dir.mkdir(parents=True, exist_ok=True)
        except (FileExistsError, NotADirectoryError, OSError) as exc:
            # A file occupying the tokens path (or a parent) would otherwise
            # surface as a raw traceback during onboarding.
            raise RuntimeError(
                f"Token storage folder {self.tokens_dir} could not be created "
                f"({exc}). Make sure storage/ is a folder, not a file."
            ) from exc
        self._secure_dir(self.tokens_dir)
        self.pointer_path = self.tokens_dir / "active.json"
        self.legacy_token_path = (
            legacy_token_path.expanduser().resolve() if legacy_token_path else None
        )

    # --- low-level helpers ---------------------------------------------------
    @staticmethod
    def _secure_dir(path: Path) -> None:
        if os.name == "posix":
            try:
                path.chmod(0o700)
            except OSError:
                pass

    @staticmethod
    def _safe_email(email: str) -> str:
        # Defensive: keep filenames flat so an account name can never escape
        # the tokens directory (no slashes / path traversal).
        return email.strip().lower().replace("/", "_").replace("\\", "_").replace("..", "_")

    def path_for(self, email: str) -> Path:
        return self.tokens_dir / f"{self._safe_email(email)}.json"

    # --- active-account pointer ---------------------------------------------
    def active_email(self) -> str | None:
        if not self.pointer_path.exists():
            return None
        try:
            data = json.loads(self.pointer_path.read_text(encoding="utf-8") or "{}")
            email = (data.get("active") or "").strip()
            return email or None
        except (json.JSONDecodeError, OSError):
            return None

    def set_active(self, email: str) -> None:
        self.pointer_path.write_text(
            json.dumps({"active": email.strip().lower()}, indent=2), encoding="utf-8"
        )
        if os.name == "posix":
            try:
                self.pointer_path.chmod(0o600)
            except OSError:
                pass

    def active_token_path(self) -> Path | None:
        email = self.active_email()
        if not email:
            return None
        path = self.path_for(email)
        return path if path.exists() else None

    # --- account listing / metadata -----------------------------------------
    def list_emails(self) -> list[str]:
        emails: list[str] = []
        for path in sorted(self.tokens_dir.glob("*.json")):
            if path.name == "active.json":
                continue
            emails.append(path.stem)
        return emails

    def _account_meta(self, email: str, scopes: list[str]) -> StoredAccount | None:
        path = self.path_for(email)
        if not path.exists():
            return None
        try:
            from google.oauth2.credentials import Credentials

            payload = json.loads(path.read_text(encoding="utf-8") or "{}")
            creds = Credentials.from_authorized_user_info(payload, scopes)
            # Report the *granted* scopes (persisted in the token file by
            # ``creds.to_json()``), not the requested set, so callers can tell
            # whether a capability (e.g. gmail.modify) was actually consented to.
            granted = list(payload.get("scopes") or creds.scopes or [])
            return StoredAccount(
                email=email,
                path=path,
                valid=bool(creds.valid),
                expired=bool(creds.expired),
                has_refresh_token=bool(creds.refresh_token),
                scopes=granted,
                last_refresh=self._last_refresh(path),
            )
        except Exception as exc:  # noqa: BLE001 - metadata read must never raise
            logger.warning("Could not read token metadata for %s: %s", email, exc)
            return None

    @staticmethod
    def _last_refresh(path: Path) -> str | None:
        try:
            mtime = path.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except OSError:
            return None

    def active_account(self, scopes: list[str]) -> StoredAccount | None:
        email = self.active_email()
        if not email:
            return None
        return self._account_meta(email, scopes)

    # --- persistence ---------------------------------------------------------
    def save(self, email: str, creds_json: str, *, make_active: bool = True) -> Path:
        path = self.path_for(email)
        path.write_text(creds_json, encoding="utf-8")
        if os.name == "posix":
            try:
                path.chmod(0o600)
            except OSError:
                pass
        if make_active:
            self.set_active(email)
        logger.info("Stored Gmail token for %s (active=%s)", email, make_active)
        return path

    def delete(self, email: str) -> bool:
        path = self.path_for(email)
        existed = path.exists()
        if existed:
            path.unlink()
        # Clear the active pointer if it referenced the removed account.
        if self.active_email() == self._safe_email(email):
            try:
                self.pointer_path.unlink()
            except OSError:
                pass
        logger.info("Removed Gmail token for %s", email)
        return existed

    # --- legacy migration ----------------------------------------------------
    def migrate_legacy(self, email: str) -> None:
        """Move a pre-existing root ``token.json`` into the new store.

        Called once the account email is known (it isn't encoded in the legacy
        file). No-op if there is nothing to migrate or the store already has an
        active account.
        """
        if self.active_token_path() is not None:
            return
        legacy = self.legacy_token_path
        if not legacy or not legacy.exists():
            return
        try:
            raw = legacy.read_text(encoding="utf-8").strip()
            if not raw:
                return
            self.save(email, raw, make_active=True)
            logger.info("Migrated legacy token.json -> storage/tokens/%s.json", email)
        except OSError as exc:
            logger.warning("Legacy token migration failed: %s", exc)
