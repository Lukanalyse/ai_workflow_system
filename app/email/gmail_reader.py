from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import Resource

from app.email.attachment_detector import detect_attachments

logger = logging.getLogger(__name__)

NOREPLY_PATTERN = re.compile(r"(?:^|[._-])(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply)(?:@|$)")
AUTOMATION_SENDER_MARKERS = {
    "linkedin.com",
    "linkedinmail.com",
    "apollo.io",
    "mailchimpapp.net",
    "list-manage.com",
    "hubspot.com",
    "salesloft.com",
    "outreach.io",
}
SUBJECT_EXCLUSION_MARKERS = {
    "newsletter",
    "digest",
    "unsubscribe",
    "special offer",
    "promo",
    "promotion",
    "webinar",
    "cold outreach",
    "new job opportunities",
    "weekly update",
}
BODY_EXCLUSION_MARKERS = {
    "unsubscribe",
    "manage preferences",
    "view in browser",
    "you are receiving this email",
    "campaign",
    "mailchimp",
    "apollo",
    "automated message",
}


@dataclass(slots=True)
class GmailMessage:
    id: str
    thread_id: str
    subject: str
    sender_email: str
    sender_name: str
    internet_message_id: str | None
    received_at: datetime
    snippet: str
    body_text: str
    label_ids: list[str]
    has_attachments: bool
    attachment_names: list[str]


@dataclass(slots=True)
class GmailReadConfig:
    only_unread: bool = True
    max_emails: int = 20
    after_date: datetime | None = None
    sender_filter: str | None = None
    exclude_promotions: bool = True
    exclude_noreply: bool = True


class GmailReader:
    def __init__(self, service: Resource, user_id: str = "me") -> None:
        self.service = service
        self.user_id = user_id

    def _build_query(self, config: GmailReadConfig) -> str:
        parts = ["in:inbox", "-in:spam", "-in:trash"]
        if config.only_unread:
            parts.append("is:unread")
        if config.exclude_promotions:
            parts.extend(["-category:promotions", "-category:social"])
        if config.after_date:
            parts.append(f"after:{config.after_date.astimezone(timezone.utc).strftime('%Y/%m/%d')}")
        if config.sender_filter:
            parts.append(f"from:{config.sender_filter.strip()}")
        return " ".join(parts)

    @staticmethod
    def _decode_part_data(data: str) -> str:
        if not data:
            return ""
        missing_padding = (-len(data)) % 4
        raw = base64.urlsafe_b64decode(data + ("=" * missing_padding))
        return raw.decode("utf-8", errors="replace")

    def _extract_body(self, payload: dict) -> tuple[str, bool]:
        mime_type = payload.get("mimeType", "")
        body_data = payload.get("body", {}).get("data", "")
        if mime_type == "text/plain" and body_data:
            return self._decode_part_data(body_data), False
        if mime_type == "text/html" and body_data:
            return self._decode_part_data(body_data), True

        plain_body = ""
        html_body = ""
        for part in payload.get("parts", []) or []:
            text, is_html = self._extract_body(part)
            if text and not is_html and not plain_body:
                plain_body = text
            if text and is_html and not html_body:
                html_body = text
        if plain_body:
            return plain_body, False
        return html_body, True

    @staticmethod
    def _headers_map(headers: list[dict[str, str]]) -> dict[str, str]:
        return {h.get("name", "").lower(): h.get("value", "") for h in headers}

    def _parse_message(self, payload: dict) -> GmailMessage:
        message_payload = payload.get("payload", {})
        headers = self._headers_map(message_payload.get("headers", []))
        raw_from = headers.get("from", "").strip()
        sender_email_match = re.search(r"<([^>]+)>", raw_from)
        sender_email = (sender_email_match.group(1) if sender_email_match else raw_from).strip().lower()
        sender_name = raw_from.replace(f"<{sender_email}>", "").strip(' "')
        body_text, _ = self._extract_body(message_payload)
        attachment_meta = detect_attachments(message_payload)
        internal_ts = int(payload.get("internalDate", "0") or "0")
        received_at = datetime.fromtimestamp(internal_ts / 1000, tz=timezone.utc) if internal_ts else datetime.now(
            tz=timezone.utc
        )
        date_header = headers.get("date", "")
        if date_header:
            try:
                parsed = parsedate_to_datetime(date_header)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                received_at = parsed.astimezone(timezone.utc)
            except Exception:
                pass

        return GmailMessage(
            id=str(payload["id"]),
            thread_id=str(payload.get("threadId", "")),
            subject=headers.get("subject", "(No subject)").strip() or "(No subject)",
            sender_email=sender_email,
            sender_name=sender_name,
            internet_message_id=headers.get("message-id"),
            received_at=received_at,
            snippet=str(payload.get("snippet", "")),
            body_text=body_text,
            label_ids=[str(v) for v in payload.get("labelIds", [])],
            has_attachments=attachment_meta.has_attachments,
            attachment_names=attachment_meta.filenames,
        )

    def list_latest_unread(self, config: GmailReadConfig) -> list[GmailMessage]:
        hard_limit = max(1, min(config.max_emails, 20))
        query = self._build_query(config)
        logger.info("Fetching latest Gmail unread messages: limit=%s query=%s", hard_limit, query)
        rows = (
            self.service.users()
            .messages()
            .list(userId=self.user_id, maxResults=hard_limit, q=query)
            .execute()
            .get("messages", [])
        )
        messages: list[GmailMessage] = []
        for row in rows:
            detail = (
                self.service.users()
                .messages()
                .get(userId=self.user_id, id=row["id"], format="full")
                .execute()
            )
            messages.append(self._parse_message(detail))
        return messages

    def evaluate_replyability(self, message: GmailMessage, *, exclude_noreply: bool = True) -> tuple[bool, str]:
        label_set = {label.upper() for label in message.label_ids}
        sender = message.sender_email.lower()
        subject = message.subject.lower()
        body = message.body_text.lower()
        snippet = message.snippet.lower()
        content = f"{subject}\n{snippet}\n{body}"

        if "SPAM" in label_set:
            return False, "gmail_spam_label"
        if "CATEGORY_PROMOTIONS" in label_set:
            return False, "gmail_promotions_label"
        if "CATEGORY_SOCIAL" in label_set and "linkedin" in content:
            return False, "linkedin_notification"
        if exclude_noreply and NOREPLY_PATTERN.search(sender):
            return False, "noreply_sender"
        if any(domain in sender for domain in AUTOMATION_SENDER_MARKERS):
            return False, "marketing_automation_sender"
        if any(marker in subject for marker in SUBJECT_EXCLUSION_MARKERS):
            return False, "marketing_or_newsletter_subject"
        if any(marker in content for marker in BODY_EXCLUSION_MARKERS):
            return False, "marketing_or_automation_body"
        if not sender or "@" not in sender:
            return False, "invalid_sender"
        if not (message.body_text.strip() or message.snippet.strip()):
            return False, "empty_content"
        return True, "replyable"
