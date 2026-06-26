"""The AI understanding layer: analyze an email once, cache it, reuse everywhere.

Every future feature (Smart Archive, AI Labels, Automatic Filing, Priority
Inbox, Suggestions) reads from :class:`EmailAnalysisService` instead of issuing
its own LLM calls. The cache is behind a small protocol so it can be swapped
(e.g. for Redis or a vector store) without touching the service or callers.

This layer never mutates Gmail — it only understands.
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from app.database.sqlite_manager import SQLiteManager
from app.llm.email_analysis import EmailAnalysis
from app.providers.base import EmailMessage
from app.services.llm_service import LLMService

logger = logging.getLogger(__name__)


class AnalysisCache(Protocol):
    """Pluggable cache for email analyses (swap the implementation freely)."""

    def get(self, message_id: str) -> EmailAnalysis | None: ...

    def get_many(self, message_ids: list[str]) -> dict[str, EmailAnalysis]: ...

    def set(self, message_id: str, analysis: EmailAnalysis) -> None: ...


def _from_dict(data: dict) -> EmailAnalysis:
    return EmailAnalysis(**data)


class SQLiteAnalysisCache:
    """Default cache backed by the ``email_ai_analysis`` table."""

    def __init__(self, sqlite: SQLiteManager) -> None:
        self._db = sqlite

    def get(self, message_id: str) -> EmailAnalysis | None:
        data = self._db.get_email_analysis(message_id)
        return _from_dict(data) if data else None

    def get_many(self, message_ids: list[str]) -> dict[str, EmailAnalysis]:
        return {
            mid: _from_dict(data)
            for mid, data in self._db.get_email_analysis_many(message_ids).items()
        }

    def set(self, message_id: str, analysis: EmailAnalysis) -> None:
        self._db.save_email_analysis(message_id, analysis.as_dict())


class EmailAnalysisService:
    """Analyze-once-then-reuse engine.

    ``analyze`` is cache-first: a previously analyzed email is returned from the
    cache and never re-sent to the LLM (unless ``force=True``). ``get_cached`` /
    ``get_cached_many`` are pure reads used by the inbox list so it can show
    analysis badges without ever triggering a paid call.
    """

    def __init__(
        self,
        *,
        llm_service: LLMService,
        cache: AnalysisCache,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._llm = llm_service
        self._cache = cache
        self._now = clock or SQLiteManager.now_iso

    def get_cached(self, message_id: str) -> EmailAnalysis | None:
        return self._cache.get(message_id)

    def get_cached_many(self, message_ids: list[str]) -> dict[str, EmailAnalysis]:
        return self._cache.get_many(message_ids)

    def analyze(self, email: EmailMessage, *, force: bool = False) -> EmailAnalysis:
        if not force:
            cached = self._cache.get(email.id)
            if cached is not None:
                return cached
        analysis = self._llm.analyze_email(email)
        analysis.analyzed_at = self._now()
        self._cache.set(email.id, analysis)
        logger.info(
            "Analyzed email %s -> %s / %s (needs_reply=%s)",
            email.id, analysis.category, analysis.priority, analysis.needs_reply,
        )
        return analysis
