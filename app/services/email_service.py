from __future__ import annotations

import logging
from dataclasses import dataclass

from app.database.sqlite_manager import SQLiteManager
from app.email.attachment_detector import AttachmentInfo
from app.email.gmail_reader import LIST_MAX_RESULTS
from app.email.replyability import ReplyabilityScorer
from app.providers.base import EmailListConfig, EmailMessage, EmailProvider

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class EmailCandidate:
    """Lightweight view of a message for the email list UI."""

    id: str
    thread_id: str
    subject: str
    sender_email: str
    sender_name: str
    received_at: str
    snippet: str
    is_unread: bool
    label_ids: list[str]
    has_attachments: bool
    attachments: list[AttachmentInfo]
    replyable: bool
    reply_reason: str
    already_processed: bool
    score: int
    classification: str
    reasons: list[str]


class EmailService:
    """Reads/filters mailbox messages via the active EmailProvider."""

    def __init__(
        self,
        provider: EmailProvider,
        sqlite: SQLiteManager,
        scorer: ReplyabilityScorer,
    ) -> None:
        self._provider = provider
        self._sqlite = sqlite
        self._scorer = scorer

    def list_candidates(
        self, *, max_emails: int = 20, status: str = "unread"
    ) -> list[EmailCandidate]:
        config = EmailListConfig(
            max_emails=max(1, min(max_emails, LIST_MAX_RESULTS)),
            status=status if status in {"unread", "read", "all"} else "unread",
        )
        messages = self._provider.list_messages(config)

        # Resolve "already processed" and "known sender" for the whole batch in
        # two queries each, rather than three SQLite round-trips per email.
        seen_messages, draft_threads = self._sqlite.seen_status_bulk(
            [m.id for m in messages], [m.thread_id for m in messages]
        )
        known_senders = self._sqlite.known_senders([m.sender_email for m in messages])

        candidates: list[EmailCandidate] = []
        for msg in messages:
            seen = msg.id in seen_messages or msg.thread_id in draft_threads
            known = (msg.sender_email or "").strip().lower() in known_senders
            # Mirror the original single-email path, which passed the combined
            # "seen" value (message- or thread-level) as the scorer's thread_seen.
            result = self._scorer.score(msg, known_sender=known, thread_seen=seen)
            labels = list(msg.label_ids)
            candidates.append(
                EmailCandidate(
                    id=msg.id,
                    thread_id=msg.thread_id,
                    subject=msg.subject,
                    sender_email=msg.sender_email,
                    sender_name=msg.sender_name,
                    received_at=msg.received_at.isoformat(),
                    snippet=msg.snippet,
                    is_unread=any(label.upper() == "UNREAD" for label in labels),
                    label_ids=labels,
                    has_attachments=msg.has_attachments,
                    attachments=list(msg.attachments),
                    replyable=result.replyable,
                    reply_reason=result.reply_reason,
                    already_processed=seen,
                    score=result.score,
                    classification=result.classification,
                    reasons=result.reasons,
                )
            )
        logger.info("Listed %d email candidates", len(candidates))
        return candidates

    def get_message(self, message_id: str) -> EmailMessage:
        return self._provider.get_message(message_id)
