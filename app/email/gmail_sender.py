from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from email.message import EmailMessage as RFC822EmailMessage

from googleapiclient.discovery import Resource

from app.email.gmail_reader import GmailMessage

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GmailSendResult:
    message_id: str
    thread_id: str


class GmailSender:
    """Sends a reply via Gmail. Only invoked on explicit, confirmed user action.

    Builds the same threaded RFC822 reply as GmailDraftCreator (In-Reply-To /
    References / threadId) so a sent message threads correctly with the original.
    """

    def __init__(self, service: Resource, user_id: str = "me") -> None:
        self.service = service
        self.user_id = user_id

    def send_reply(self, original_email: GmailMessage, body: str) -> GmailSendResult:
        reply = RFC822EmailMessage()
        reply["To"] = original_email.sender_email
        reply["Subject"] = f"Re: {original_email.subject}"
        if original_email.internet_message_id:
            reply["In-Reply-To"] = original_email.internet_message_id
            reply["References"] = original_email.internet_message_id
        reply.set_content(body)

        raw = base64.urlsafe_b64encode(reply.as_bytes()).decode("utf-8")
        payload = {"raw": raw, "threadId": original_email.thread_id}
        logger.info(
            "Sending Gmail reply for message_id=%s thread_id=%s",
            original_email.id,
            original_email.thread_id,
        )
        sent = self.service.users().messages().send(userId=self.user_id, body=payload).execute()
        return GmailSendResult(
            message_id=str(sent.get("id", "")),
            thread_id=str(sent.get("threadId", original_email.thread_id)),
        )
