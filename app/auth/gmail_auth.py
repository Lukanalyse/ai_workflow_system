from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GmailAuthManager:
    """Handles Gmail OAuth token lifecycle and reuses the existing token storage flow."""

    def __init__(self, *, credentials_path: Path, token_path: Path, scopes: list[str]) -> None:
        self.credentials_path = credentials_path.expanduser().resolve()
        self.token_path = token_path.expanduser().resolve()
        self.scopes = scopes
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_credentials(self) -> Any | None:
        if not self.token_path.exists():
            return None
        try:
            raw = self.token_path.read_text(encoding="utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                "token.json is in an unsupported binary format. Delete token.json and re-run OAuth login."
            ) from exc
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                from google.oauth2.credentials import Credentials

                return Credentials.from_authorized_user_info(payload, self.scopes)
        except json.JSONDecodeError as exc:
            raise RuntimeError("token.json is invalid JSON. Delete token.json and re-run OAuth login.") from exc
        return None

    def _save_credentials(self, creds: Any) -> None:
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        self.token_path.chmod(0o600)

    def load_credentials(self) -> Any | None:
        """Return valid credentials without ever launching an interactive flow.

        Refreshes an expired-but-refreshable token (and persists it). Returns
        ``None`` when there is no usable token — the web app then asks the user
        to (re)connect Gmail through the OAuth UI rather than opening a browser
        on the server.
        """
        from google.auth.transport.requests import Request

        creds = self._load_credentials()
        if not creds:
            return None
        has_scopes = bool(creds.has_scopes(self.scopes))
        if creds.expired and creds.refresh_token and has_scopes:
            logger.info("Refreshing Gmail OAuth token")
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001 - surface as "needs reconnect"
                logger.warning("Token refresh failed: %s", exc)
                return None
            self._save_credentials(creds)
            return creds
        if creds.valid and has_scopes:
            return creds
        return None

    def get_credentials(self) -> Any:
        """Return valid credentials. Never launches a browser or local server.

        The old interactive desktop browser flow has been removed entirely:
        authorization is now a standard web OAuth flow handled by the FastAPI
        server (Settings → Gmail → "Connect Gmail", see
        :mod:`app.auth.oauth_flow`). When no usable token exists this raises with
        guidance instead of opening a browser — mandatory inside Docker, where no
        browser is available.
        """
        creds = self.load_credentials()
        if creds is not None:
            return creds
        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Gmail OAuth credentials file not found at {self.credentials_path}. "
                "Add it from the web app (Settings → Gmail)."
            )
        raise RuntimeError(
            "Gmail is not connected. Open the web app and connect Gmail from "
            "Settings → Gmail. No browser is ever launched on the server."
        )


def build_auth_manager(settings: Any) -> "GmailAuthManager":
    """Construct a GmailAuthManager bound to the active account's token.

    Resolves the token written by the web OAuth flow
    (``storage/tokens/<email>.json``), falling back to the legacy
    ``token.json``. Shared by the web provider, CLI, and Streamlit UI so every
    entry point reads the same web-connected token — and none can launch a
    browser.
    """
    from app.auth.token_store import TokenStore

    gmail = settings.gmail
    store = TokenStore(gmail.tokens_dir, legacy_token_path=gmail.token_path)
    token_path = store.active_token_path() or gmail.token_path
    return GmailAuthManager(
        credentials_path=gmail.credentials_path,
        token_path=token_path,
        scopes=gmail.scopes,
    )
