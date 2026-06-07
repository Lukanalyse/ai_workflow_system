from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Google sometimes returns a superset of the requested scopes (e.g. it adds
# the `openid` scope). Without this, oauthlib raises "Scope has changed".
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# Pending authorization requests, keyed by the OAuth `state` value. Each entry
# lives only between "Connect Gmail" and the browser redirect back. Entries
# expire so a dropped flow never leaks state forever.
_PENDING_TTL_SECONDS = 600


@dataclass(slots=True)
class _Pending:
    created_at: float
    # PKCE code_verifier generated when the consent URL was built. It MUST be
    # replayed on the token exchange or Google rejects it with
    # "invalid_grant: Missing code verifier".
    code_verifier: str


def _short(state: str | None) -> str:
    """A non-sensitive, log-safe prefix of the opaque state value."""
    s = state or ""
    return f"{s[:8]}…" if len(s) > 8 else (s or "—")


class GmailOAuthFlow:
    """Server-side Gmail OAuth (web redirect) flow with PKCE.

    This drives the standard authorization-code-with-PKCE flow through our own
    FastAPI callback. It never opens a browser or starts a local server on the
    host, so it works identically on a laptop or inside Docker — the user only
    ever sees Google's consent screen in their own browser, then Google
    redirects back to ``/api/gmail/callback``.

    The PKCE ``code_verifier`` is generated when the consent URL is built and
    **persisted** (keyed by ``state``) so the callback — which may be served by
    a different worker or after a container restart — can replay the exact same
    verifier on the token exchange. Without that, Google returns
    ``invalid_grant: Missing code verifier``.
    """

    def __init__(
        self,
        *,
        credentials_path: Path,
        scopes: list[str],
        redirect_uri: str,
        state_path: Path | None = None,
    ) -> None:
        self.credentials_path = credentials_path.expanduser().resolve()
        self.scopes = scopes
        self.redirect_uri = redirect_uri
        # Where pending {state -> verifier} is persisted across requests and
        # restarts. ``None`` keeps it in-memory only (used in tests).
        self._state_path = state_path
        self._pending: dict[str, _Pending] = {}
        self._load_state()

    def credentials_available(self) -> bool:
        return self.credentials_path.exists()

    def _build_flow(self, state: str | None = None) -> Any:
        from google_auth_oauthlib.flow import Flow

        if not self.credentials_path.exists():
            raise FileNotFoundError(
                "Google OAuth client file not found. Add your Google Cloud "
                "credentials.json from the Gmail settings page first."
            )
        return Flow.from_client_secrets_file(
            str(self.credentials_path),
            scopes=self.scopes,
            redirect_uri=self.redirect_uri,
            state=state,
        )

    # --- Pending-state persistence ------------------------------------------
    def _load_state(self) -> None:
        """Load persisted pending entries. Never raises."""
        if self._state_path is None or not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read OAuth state file (%s); starting empty.", exc)
            return
        loaded: dict[str, _Pending] = {}
        for state, entry in (raw or {}).items():
            try:
                loaded[state] = _Pending(
                    created_at=float(entry["created_at"]),
                    code_verifier=str(entry["code_verifier"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        self._pending = loaded

    def _save_state(self) -> None:
        """Persist pending entries atomically with private perms. Never raises."""
        if self._state_path is None:
            return
        payload = {
            state: {"created_at": p.created_at, "code_verifier": p.code_verifier}
            for state, p in self._pending.items()
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            if os.name == "posix":
                try:
                    tmp.chmod(0o600)
                except OSError:
                    pass
            os.replace(tmp, self._state_path)
        except OSError as exc:
            # Persistence is best-effort; the in-memory dict still works within
            # a single process, so a write failure must not break OAuth.
            logger.warning("Could not persist OAuth state (%s).", exc)

    def _prune(self) -> None:
        now = time.time()
        stale = [s for s, p in self._pending.items() if now - p.created_at > _PENDING_TTL_SECONDS]
        for s in stale:
            self._pending.pop(s, None)

    # --- Flow ----------------------------------------------------------------
    def authorization_url(self) -> str:
        """Return the Google consent URL the UI should open in a new window."""
        # Refresh from disk first so we don't clobber entries written by another
        # worker, then prune expired ones.
        self._load_state()
        self._prune()
        flow = self._build_flow()
        auth_url, state = flow.authorization_url(
            access_type="offline",  # request a refresh token
            include_granted_scopes="true",
            prompt="consent",  # always return a refresh token, even on re-auth
        )
        # google-auth-oauthlib auto-generates this during authorization_url();
        # capture it so the callback can replay the SAME verifier.
        code_verifier = getattr(flow, "code_verifier", None)
        if not code_verifier:
            # Should never happen (autogenerate_code_verifier defaults True), but
            # if it does the exchange would fail with "Missing code verifier".
            logger.error(
                "OAuth connect: no PKCE code_verifier produced (state=%s) — "
                "the token exchange will fail.",
                _short(state),
            )
        self._pending[state] = _Pending(created_at=time.time(), code_verifier=code_verifier or "")
        self._save_state()
        logger.info(
            "OAuth connect: consent URL generated (state=%s, pkce=%s, redirect_uri=%s, scopes=%d)",
            _short(state),
            bool(code_verifier),
            self.redirect_uri,
            len(self.scopes),
        )
        return auth_url

    def exchange_code(self, *, code: str, state: str) -> tuple[Any, str]:
        """Exchange the callback code for credentials and the account email.

        Returns (credentials, email). Raises on an unknown state or a failed
        token exchange. Replays the PKCE verifier captured at /connect.
        """
        # Reload so we see entries written before a restart / by another worker.
        self._load_state()
        self._prune()
        pending = self._pending.pop(state, None)
        self._save_state()
        if pending is None:
            logger.warning(
                "OAuth callback: unknown or expired state=%s (no pending verifier).",
                _short(state),
            )
            raise PermissionError("Unknown or expired OAuth state. Please retry the connection.")

        flow = self._build_flow(state=state)
        # Replay the original verifier so the token request includes it. This is
        # the line that fixes "invalid_grant: Missing code verifier".
        if pending.code_verifier:
            flow.code_verifier = pending.code_verifier
        logger.info(
            "OAuth callback: exchanging authorization code (state=%s, pkce=%s, redirect_uri=%s)",
            _short(state),
            bool(pending.code_verifier),
            self.redirect_uri,
        )
        try:
            flow.fetch_token(code=code)
        except Exception as exc:  # noqa: BLE001 - log a safe summary, then re-raise
            # Never log the code, verifier, or token — only the error type/text.
            logger.error("OAuth callback: token exchange failed (state=%s): %s", _short(state), exc)
            raise
        creds = flow.credentials
        email = self._fetch_email(creds)
        logger.info(
            "OAuth callback: exchange succeeded for %s (refresh_token=%s)",
            email,
            bool(getattr(creds, "refresh_token", None)),
        )
        return creds, email

    @staticmethod
    def _fetch_email(creds: Any) -> str:
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=creds)
        profile = service.users().getProfile(userId="me").execute()
        email = (profile.get("emailAddress") or "").strip().lower()
        if not email:
            raise RuntimeError("Could not read the Gmail account email after authorization.")
        return email
