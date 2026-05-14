from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from app.auth.microsoft_auth import MicrosoftAuthManager
from app.email.filters import EmailFilterConfig, build_graph_filter, match_filters

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EmailMessage:
    id: str
    subject: str
    sender_email: str
    sender_name: str
    received_at: datetime
    body_content: str
    body_content_type: str
    is_read: bool
    internet_message_id: str | None = None


class OutlookEmailReader:
    """Reads messages from Microsoft Graph mailbox."""

    def __init__(self, auth_manager: MicrosoftAuthManager, graph_base_url: str) -> None:
        self.auth_manager = auth_manager
        self.graph_base_url = graph_base_url.rstrip("/")

    def _client(self) -> httpx.Client:
        token = self.auth_manager.get_access_token()
        return httpx.Client(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

    @staticmethod
    def _parse_message(payload: dict[str, Any]) -> EmailMessage:
        sender = payload.get("from", {}).get("emailAddress", {})
        body = payload.get("body", {})
        return EmailMessage(
            id=payload["id"],
            subject=payload.get("subject") or "(No subject)",
            sender_email=sender.get("address", "").lower(),
            sender_name=sender.get("name", ""),
            received_at=datetime.fromisoformat(payload["receivedDateTime"].replace("Z", "+00:00")),
            body_content=body.get("content", ""),
            body_content_type=body.get("contentType", "Text"),
            is_read=bool(payload.get("isRead", True)),
            internet_message_id=payload.get("internetMessageId"),
        )

    def fetch_inbox_messages(self, filters: EmailFilterConfig, limit: int = 25) -> list[EmailMessage]:
        params: dict[str, str] = {
            "$top": str(limit),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,body,isRead,internetMessageId",
        }
        graph_filter = build_graph_filter(filters)
        if graph_filter:
            params["$filter"] = graph_filter

        with self._client() as client:
            logger.info("Fetching inbox messages with filter: %s", graph_filter or "none")
            response = client.get(f"{self.graph_base_url}/me/mailFolders/Inbox/messages", params=params)
            response.raise_for_status()
            rows = response.json().get("value", [])

        messages: list[EmailMessage] = []
        for row in rows:
            msg = self._parse_message(row)
            if match_filters(
                filters,
                sender=msg.sender_email,
                subject=msg.subject,
                body=msg.body_content,
                received_at=msg.received_at,
            ):
                messages.append(msg)
        return messages

