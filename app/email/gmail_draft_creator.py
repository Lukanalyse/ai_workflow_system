from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from email.message import EmailMessage as RFC822EmailMessage

from googleapiclient.discovery import Resource

from app.email.gmail_reader import GmailMessage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GmailDraftResult:
    draft_id: str
    message_id: str
    thread_id: str


class GmailDraftCreator:
    """Creates Gmail drafts only; never sends automatically."""

    def __init__(self, service: Resource, user_id: str = "me") -> None:
        self.service = service
        self.user_id = user_id

    def create_draft(self, original_email: GmailMessage, draft_body: str) -> GmailDraftResult:
        reply = RFC822EmailMessage()
        reply["To"] = original_email.sender_email
        reply["Subject"] = f"Re: {original_email.subject}"
        if original_email.internet_message_id:
            reply["In-Reply-To"] = original_email.internet_message_id
            reply["References"] = original_email.internet_message_id
        reply.set_content(draft_body)

        raw = base64.urlsafe_b64encode(reply.as_bytes()).decode("utf-8")
        payload = {
            "message": {
                "raw": raw,
                "threadId": original_email.thread_id,
            }
        }
        logger.info("Creating Gmail draft for message_id=%s thread_id=%s", original_email.id, original_email.thread_id)
        created = self.service.users().drafts().create(userId=self.user_id, body=payload).execute()
        message = created.get("message", {})
        return GmailDraftResult(
            draft_id=str(created.get("id", "")),
            message_id=str(message.get("id", "")),
            thread_id=str(message.get("threadId", original_email.thread_id)),
        )

