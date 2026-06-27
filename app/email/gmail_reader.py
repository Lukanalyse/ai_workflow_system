from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import Resource

from app.email.attachment_detector import AttachmentInfo, detect_attachments

logger = logging.getLogger(__name__)

# Safety ceiling for a single listing (incl. the "all" option). Prevents a
# pathological full-mailbox scan from issuing thousands of per-message gets.
LIST_MAX_RESULTS = 500
# Gmail's messages.list returns at most 500 ids per page.
_GMAIL_PAGE_SIZE = 500
# Messages per batched messages.get round-trip. Gmail recommends <=100 sub-
# requests per batch; 50 keeps each batch payload comfortably small.
_BATCH_GET_SIZE = 50

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
    attachments: list[AttachmentInfo]


@dataclass(slots=True)
class GmailReadConfig:
    only_unread: bool = True
    max_emails: int = 20
    after_date: datetime | None = None
    sender_filter: str | None = None
    exclude_promotions: bool = True
    exclude_noreply: bool = True
    # "unread" | "read" | "all". When None, falls back to only_unread so older
    # callers keep their previous behavior.
    status: str | None = None


class GmailReader:
    def __init__(self, service: Resource, user_id: str = "me") -> None:
        self.service = service
        self.user_id = user_id

    def _build_query(self, config: GmailReadConfig) -> str:
        parts = ["in:inbox", "-in:spam", "-in:trash"]
        status = (config.status or ("unread" if config.only_unread else "all")).lower()
        if status == "unread":
            parts.append("is:unread")
        elif status == "read":
            parts.append("is:read")
        # "all": no read-status constraint.
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
        try:
            missing_padding = (-len(data)) % 4
            raw = base64.urlsafe_b64decode(data + ("=" * missing_padding))
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""

    def _extract_body(self, payload: dict) -> tuple[str, bool]:
        mime_type = payload.get("mimeType", "")
        filename = str(payload.get("filename", "")).strip()
        attachment_id = (payload.get("body", {}) or {}).get("attachmentId")
        # Never parse attachment parts into LLM input, even if they contain text/* mime types.
        if filename or attachment_id:
            return "", False
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
            attachments=list(attachment_meta.attachments),
        )

    def get_message(self, message_id: str) -> GmailMessage:
        detail = (
            self.service.users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="full")
            .execute()
        )
        return self._parse_message(detail)

    def _get_full(self, message_id: str) -> dict:
        return (
            self.service.users()
            .messages()
            .get(userId=self.user_id, id=message_id, format="full")
            .execute()
        )

    def _sequential_get(self, ids: list[str]) -> list[GmailMessage]:
        return [self._parse_message(self._get_full(mid)) for mid in ids]

    def _batch_get(self, ids: list[str]) -> list[GmailMessage]:
        """Fetch many messages in a few HTTP round-trips via Gmail's batch API.

        Each ``messages.get`` keeps ``format="full"`` so the parsed result — and
        therefore replyability — is byte-for-byte identical to the per-id path;
        only the number of round-trips changes (N gets -> ceil(N/50) batches).
        Responses can arrive out of order, so they are reassembled into the
        original ``ids`` order. A per-message failure is retried individually,
        preserving the previous "raise on a bad get" behaviour.
        """
        results: dict[str, dict] = {}
        errors: dict[str, Exception] = {}

        def _cb(request_id, response, exception):
            if exception is not None:
                errors[request_id] = exception
            else:
                results[request_id] = response

        for start in range(0, len(ids), _BATCH_GET_SIZE):
            chunk = ids[start : start + _BATCH_GET_SIZE]
            batch = self.service.new_batch_http_request()
            for mid in chunk:
                batch.add(
                    self.service.users().messages().get(
                        userId=self.user_id, id=mid, format="full"
                    ),
                    request_id=mid,
                    callback=_cb,
                )
            batch.execute()

        # Retry the (rare) per-message failures one by one; a still-failing get
        # raises, exactly as the old sequential path did.
        for mid in list(errors):
            results[mid] = self._get_full(mid)

        return [self._parse_message(results[mid]) for mid in ids if mid in results]

    def _fetch_messages(self, ids: list[str]) -> list[GmailMessage]:
        """Fetch full messages for ``ids`` (batched when the client supports it).

        Falls back to sequential gets if the service has no batch support (e.g.
        a stub in tests) or if the batch mechanism itself fails, so the listing
        never breaks — it just gets slower in that degraded case.
        """
        if not ids:
            return []
        if not hasattr(self.service, "new_batch_http_request"):
            return self._sequential_get(ids)
        try:
            return self._batch_get(ids)
        except Exception:  # noqa: BLE001 - batch transport problem -> safe fallback
            logger.warning(
                "Batched message fetch failed; falling back to sequential gets",
                exc_info=True,
            )
            return self._sequential_get(ids)

    def _collect_ids(self, query: str, target: int) -> list[str]:
        """Page through messages.list until `target` ids are gathered or the
        result set is exhausted. Only ids are pulled here (cheap); bodies are
        fetched separately so we never over-fetch beyond `target`."""
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < target:
            resp = (
                self.service.users()
                .messages()
                .list(
                    userId=self.user_id,
                    maxResults=min(_GMAIL_PAGE_SIZE, target - len(ids)),
                    q=query,
                    pageToken=page_token,
                )
                .execute()
            )
            ids.extend(str(r["id"]) for r in resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids[:target]

    def get_label_counts(self, label_id: str) -> tuple[int, int]:
        """Return (total, unread) message counts for a label.

        Uses ``labels.get`` (one cheap call), so the Archive folder list never
        has to enumerate a label's messages just to show a count.
        """
        data = (
            self.service.users()
            .labels()
            .get(userId=self.user_id, id=label_id)
            .execute()
        )
        total = int(data.get("messagesTotal", 0) or 0)
        unread = int(data.get("messagesUnread", 0) or 0)
        return total, unread

    def get_label_counts_many(self, label_ids: list[str]) -> dict[str, tuple[int, int]]:
        """Return ``{label_id: (total, unread)}`` for many labels at once.

        Replaces the Archive folder grid's per-label ``labels.get`` (N sequential
        round-trips) with one batched call (ceil(N/50)). A per-label failure
        degrades that one label to ``(0, 0)`` — never hiding the rest — exactly
        like the previous per-label try/except in ArchiveService.list_folders.
        """
        ids = [lid for lid in dict.fromkeys(label_ids) if lid]
        if not ids:
            return {}
        if not hasattr(self.service, "new_batch_http_request"):
            return {lid: self._safe_label_counts(lid) for lid in ids}
        try:
            return self._batch_label_counts(ids)
        except Exception:  # noqa: BLE001 - batch transport problem -> safe fallback
            logger.warning(
                "Batched label counts failed; falling back to sequential gets",
                exc_info=True,
            )
            return {lid: self._safe_label_counts(lid) for lid in ids}

    def _safe_label_counts(self, label_id: str) -> tuple[int, int]:
        try:
            return self.get_label_counts(label_id)
        except Exception:  # noqa: BLE001 - one bad label must not hide the rest
            logger.warning("Failed to read counts for label %s", label_id, exc_info=True)
            return 0, 0

    def _batch_label_counts(self, label_ids: list[str]) -> dict[str, tuple[int, int]]:
        results: dict[str, tuple[int, int]] = {}
        errors: list[str] = []

        def _cb(request_id, response, exception):
            if exception is not None:
                errors.append(request_id)
            else:
                results[request_id] = (
                    int((response or {}).get("messagesTotal", 0) or 0),
                    int((response or {}).get("messagesUnread", 0) or 0),
                )

        for start in range(0, len(label_ids), _BATCH_GET_SIZE):
            chunk = label_ids[start : start + _BATCH_GET_SIZE]
            batch = self.service.new_batch_http_request()
            for lid in chunk:
                batch.add(
                    self.service.users().labels().get(userId=self.user_id, id=lid),
                    request_id=lid,
                    callback=_cb,
                )
            batch.execute()

        # Retry the (rare) per-label failures individually; degrade to (0,0) if
        # a retry still fails, so a single bad label never breaks the grid.
        for lid in errors:
            results[lid] = self._safe_label_counts(lid)
        return results

    def list_by_label(
        self, label_id: str, *, page_size: int = 25, page_token: str | None = None
    ) -> tuple[list[GmailMessage], str | None]:
        """Return one page of messages carrying ``label_id`` plus the next token.

        Filters by ``labelIds`` rather than a free-text query so a label whose
        name contains spaces or punctuation still resolves exactly. Only the page
        of ids is listed; bodies are fetched per id (same cost model as the inbox
        reader), keeping the Archive workspace from ever loading a whole label.
        """
        size = max(1, min(int(page_size), _GMAIL_PAGE_SIZE))
        resp = (
            self.service.users()
            .messages()
            .list(
                userId=self.user_id,
                labelIds=[label_id],
                maxResults=size,
                pageToken=page_token or None,
                includeSpamTrash=False,
            )
            .execute()
        )
        ids = [str(r["id"]) for r in resp.get("messages", [])]
        next_token = resp.get("nextPageToken")
        return self._fetch_messages(ids), next_token

    def list_latest_unread(self, config: GmailReadConfig) -> list[GmailMessage]:
        target = max(1, min(config.max_emails, LIST_MAX_RESULTS))
        query = self._build_query(config)
        logger.info("Fetching Gmail messages: target=%s query=%s", target, query)
        ids = self._collect_ids(query, target)
        return self._fetch_messages(ids)

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
