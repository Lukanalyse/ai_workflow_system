from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import msal

logger = logging.getLogger(__name__)


class MicrosoftAuthManager:
    """Handles OAuth2 and token refresh lifecycle via MSAL token cache."""

    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        scopes: list[str],
        token_cache_path: Path,
        authority_host: str = "https://login.microsoftonline.com",
    ) -> None:
        self.client_id = client_id
        self.scopes = scopes
        self.authority = f"{authority_host.rstrip('/')}/{tenant_id}"
        self.token_cache_path = token_cache_path.expanduser().resolve()
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = msal.SerializableTokenCache()
        self._load_cache()
        self._app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority=self.authority,
            token_cache=self._cache,
        )

    def _load_cache(self) -> None:
        if self.token_cache_path.exists():
            self._cache.deserialize(self.token_cache_path.read_text(encoding="utf-8"))

    def _save_cache_if_changed(self) -> None:
        if self._cache.has_state_changed:
            self.token_cache_path.write_text(self._cache.serialize(), encoding="utf-8")
            self.token_cache_path.chmod(0o600)

    def _interactive_acquire(self) -> dict[str, Any]:
        logger.info("Starting Microsoft device flow authentication")
        flow = self._app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start device flow: {flow}")
        logger.info("Authenticate in browser: %s", flow["message"])
        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(f"Authentication failed: {result.get('error_description', result)}")
        return result

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing automatically via cache."""
        accounts = self._app.get_accounts()
        result: dict[str, Any] | None = None
        if accounts:
            logger.debug("Attempting silent token acquisition")
            result = self._app.acquire_token_silent(self.scopes, account=accounts[0])
        if not result or "access_token" not in result:
            result = self._interactive_acquire()
        self._save_cache_if_changed()
        return str(result["access_token"])

