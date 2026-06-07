from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config.settings import AppSettings
from app.database.sqlite_manager import (
    GmailProcessedEmailRecord,
    SQLiteManager,
    UsageEventRecord,
)
from app.providers.base import EmailMessage, EmailProvider, SentResult
from app.security.startup_checks import sanitize_persisted_fields
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GeneratedDraft:
    summary: str
    draft: str


class DraftService:
    """Generates AI drafts and saves them into the mailbox (draft-only)."""

    def __init__(
        self,
        *,
        llm_service: LLMService,
        provider: EmailProvider,
        sqlite: SQLiteManager,
        settings: AppSettings,
    ) -> None:
        self._llm = llm_service
        self._provider = provider
        self._sqlite = sqlite
        self._settings = settings

    def generate(
        self,
        email: EmailMessage,
        *,
        tone: str | None = None,
        language: str | None = None,
        run_id: str | None = None,
    ) -> GeneratedDraft:
        summary = self._llm.summarize(email, run_id=run_id)
        draft = self._llm.generate_draft(email, tone=tone, language=language, run_id=run_id)
        logger.info("Generated draft for message_id=%s", email.id)
        return GeneratedDraft(summary=summary, draft=draft)

    def send(self, email: EmailMessage, *, body: str) -> SentResult:
        """Send a reply. Provider enforces the ENABLE_EMAIL_SENDING gate too."""
        result = self._provider.send_reply(email, body)
        now = self._sqlite.now_iso()
        # Record a zero-token 'send' usage event so the dashboard can count
        # "drafts sent" through the same usage pipeline as everything else.
        try:
            self._sqlite.record_usage_event(
                UsageEventRecord(
                    timestamp=now,
                    provider=self._provider.name,
                    model="-",
                    operation="send",
                    email_message_id=email.id,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    estimated_cost=0.0,
                    currency="USD",
                    run_id=None,
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record send usage event for %s", email.id)
        logger.info("Sent reply message_id=%s for original=%s", result.message_id, email.id)
        return result

    def save(self, email: EmailMessage, *, draft_text: str, summary: str | None = None) -> str:
        result = self._provider.create_draft(email, draft_text)
        now = self._sqlite.now_iso()
        snippet_db, summary_db, draft_db = sanitize_persisted_fields(
            snippet=email.snippet,
            summary=summary,
            draft_text=draft_text,
            settings=self._settings,
        )
        self._sqlite.save_gmail_processed_email(
            GmailProcessedEmailRecord(
                message_id=email.id,
                thread_id=email.thread_id,
                subject=email.subject,
                sender=email.sender_email,
                received_at=email.received_at.isoformat(),
                snippet=snippet_db,
                processed_status="processed",
                draft_created=True,
                draft_id=result.draft_id,
                skip_reason=None,
                summary=summary_db,
                intent_label=None,
                urgency_score=None,
                confidence_score=None,
                draft_text=draft_db,
                created_at=now,
                updated_at=now,
            )
        )
        logger.info("Saved Gmail draft id=%s for message_id=%s", result.draft_id, email.id)
        return result.draft_id
