from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)


class GmailAuthManager:
    """Handles Gmail OAuth token lifecycle and reuses the existing token storage flow."""

    def __init__(self, *, credentials_path: Path, token_path: Path, scopes: list[str]) -> None:
        self.credentials_path = credentials_path.expanduser().resolve()
        self.token_path = token_path.expanduser().resolve()
        self.scopes = scopes
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

    def _load_credentials(self) -> Credentials | None:
        if not self.token_path.exists():
            return None
        raw = self.token_path.read_bytes()
        if not raw:
            return None
        try:
            # Existing local flow stores pickled Credentials in token.json.
            creds = pickle.loads(raw)
            if isinstance(creds, Credentials):
                return creds
        except Exception:
            pass

        try:
            payload = json.loads(raw.decode("utf-8"))
            if isinstance(payload, dict):
                return Credentials.from_authorized_user_info(payload, self.scopes)
        except Exception:
            pass
        return None

    def _save_credentials(self, creds: Credentials) -> None:
        self.token_path.write_bytes(pickle.dumps(creds))
        self.token_path.chmod(0o600)

    def get_credentials(self) -> Credentials:
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

