from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.auth.microsoft_auth import MicrosoftAuthManager
from app.email.read_emails import EmailMessage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DraftResult:
    draft_id: str
    web_link: str | None


class OutlookDraftCreator:
    """Creates Outlook drafts in Drafts folder; never sends automatically."""

    def __init__(self, auth_manager: MicrosoftAuthManager, graph_base_url: str) -> None:
        self.auth_manager = auth_manager
        self.graph_base_url = graph_base_url.rstrip("/")

    def _client(self) -> httpx.Client:
        token = self.auth_manager.get_access_token()
        return httpx.Client(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )

    def create_draft(self, original_email: EmailMessage, draft_body: str) -> DraftResult:
        payload: dict[str, Any] = {
            "subject": f"Re: {original_email.subject}",
            "body": {
                "contentType": "Text",
                "content": draft_body,
            },
            "toRecipients": [
                {
                    "emailAddress": {
                        "address": original_email.sender_email,
                        "name": original_email.sender_name or original_email.sender_email,
                    }
                }
            ],
        }
        with self._client() as client:
            logger.info("Creating draft for email_id=%s", original_email.id)
            response = client.post(f"{self.graph_base_url}/me/messages", json=payload)
            response.raise_for_status()
            draft = response.json()
            return DraftResult(draft_id=draft["id"], web_link=draft.get("webLink"))

