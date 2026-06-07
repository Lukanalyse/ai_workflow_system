from __future__ import annotations

import logging
from dataclasses import dataclass

from app.database.sqlite_manager import SQLiteManager
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
    has_attachments: bool
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

    def list_candidates(self, *, max_emails: int = 20) -> list[EmailCandidate]:
        config = EmailListConfig(max_emails=max(1, min(max_emails, 100)))
        messages = self._provider.list_messages(config)
        candidates: list[EmailCandidate] = []
        for msg in messages:
            seen, _ = self._sqlite.already_processed_gmail(msg.id, msg.thread_id)
            known = self._sqlite.sender_seen(msg.sender_email)
            result = self._scorer.score(msg, known_sender=known, thread_seen=seen)
            candidates.append(
                EmailCandidate(
                    id=msg.id,
                    thread_id=msg.thread_id,
                    subject=msg.subject,
                    sender_email=msg.sender_email,
                    sender_name=msg.sender_name,
                    received_at=msg.received_at.isoformat(),
                    snippet=msg.snippet,
                    has_attachments=msg.has_attachments,
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
