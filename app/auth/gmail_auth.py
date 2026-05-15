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

    def get_credentials(self) -> Any:
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.credentials_path.exists():
            raise FileNotFoundError(
                f"Gmail OAuth credentials file not found at {self.credentials_path}. "
                "Add credentials/credentials.json from Google Cloud."
            )
        creds = self._load_credentials()
        has_scopes = bool(creds and creds.has_scopes(self.scopes))

        if creds and creds.expired and creds.refresh_token and has_scopes:
            logger.info("Refreshing Gmail OAuth token")
            creds.refresh(Request())
            self._save_credentials(creds)
            return creds

        if creds and creds.valid and has_scopes:
            return creds

        logger.info("Starting Gmail OAuth consent flow")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), self.scopes)
        creds = flow.run_local_server(port=0)
        self._save_credentials(creds)
        return creds
