"""Smart Archive — the AI Filing engine.

Files selected emails into a Gmail label derived from their (already cached)
AI analysis, then archives them. It reads ONLY the Phase 4 analysis cache and
NEVER issues an LLM call — structurally, it has no LLM dependency, only a cache
reader and the MailboxService.

The label is chosen by a pluggable ``label_resolver`` (analysis -> label name).
The default maps category -> label, but the seam is deliberately open so future
rules can return nested labels (e.g. "Finance/Taxes") for Auto Filing / AI Rules
/ Inbox Cleanup without changing this service.

Atomicity: each label group is filed with a single ``apply_label(archive=True)``
call, which adds the label and removes INBOX in one Gmail batchModify. That is
atomic per group, so an email is never left labelled-but-not-archived (or vice
versa) — a failed group simply stays untouched and is reported.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Protocol

from app.llm.email_analysis import EmailAnalysis
from app.services.mailbox_service import MailboxService

logger = logging.getLogger(__name__)


class CacheReader(Protocol):
    """Just the read side of the analysis cache (no LLM, no analyze)."""

    def get_cached_many(self, message_ids: list[str]) -> dict[str, EmailAnalysis]: ...


def default_label_resolver(analysis: EmailAnalysis) -> str:
    """Map an analysis to a Gmail label name. Default: the category itself."""
    return analysis.category or "Other"


@dataclass(slots=True)
class ArchivePlanItem:
    label: str
    message_ids: list[str]

    @property
    def count(self) -> int:
        return len(self.message_ids)


@dataclass(slots=True)
class ArchivePlan:
    items: list[ArchivePlanItem]
    total_selected: int
    skipped_unanalyzed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "items": [{"label": i.label, "count": i.count} for i in self.items],
            "total_selected": self.total_selected,
            "analyzed": sum(i.count for i in self.items),
            "skipped_unanalyzed": len(self.skipped_unanalyzed),
        }


@dataclass(slots=True)
class ArchiveResult:
    archived: int
    labels_created: list[str]
    by_label: dict[str, int]
    skipped_unanalyzed: int
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "archived": self.archived,
            "labels_created": self.labels_created,
            "by_label": self.by_label,
            "skipped_unanalyzed": self.skipped_unanalyzed,
            "failed": len(self.failures),
            "failures": self.failures,
        }


class SmartArchiveService:
    def __init__(
        self,
        *,
        analysis_cache: CacheReader,
        mailbox: MailboxService,
        label_resolver: Callable[[EmailAnalysis], str] = default_label_resolver,
    ) -> None:
        self._cache = analysis_cache
        self._mailbox = mailbox
        self._resolver = label_resolver

    def plan(self, message_ids: list[str]) -> ArchivePlan:
        """Group selected emails by target label using cached analysis only.

        Emails without a cached analysis are skipped (never analyzed here).
        """
        ids = [m for m in dict.fromkeys(message_ids) if m]
        analyses = self._cache.get_cached_many(ids)
        groups: dict[str, list[str]] = {}
        skipped: list[str] = []
        for mid in ids:
            analysis = analyses.get(mid)
            if analysis is None:
                skipped.append(mid)
                continue
            label = (self._resolver(analysis) or "Other").strip() or "Other"
            groups.setdefault(label, []).append(mid)
        items = [ArchivePlanItem(label=label, message_ids=mids) for label, mids in groups.items()]
        items.sort(key=lambda i: (-i.count, i.label.lower()))
        return ArchivePlan(items=items, total_selected=len(ids), skipped_unanalyzed=skipped)

    def execute(self, message_ids: list[str]) -> ArchiveResult:
        """File-and-archive each label group. Reuses labels; creates missing ones once."""
        plan = self.plan(message_ids)
        if not plan.items:
            return ArchiveResult(
                archived=0, labels_created=[], by_label={},
                skipped_unanalyzed=len(plan.skipped_unanalyzed),
            )

        # One labels listing; reuse existing labels, create only what's missing.
        # (A missing-scope error surfaces here, before any change is made.)
        existing = {l["name"].lower(): l["id"] for l in self._mailbox.list_labels()}
        created: list[str] = []
        by_label: dict[str, int] = {}
        failures: list[dict] = []
        archived = 0

        for item in plan.items:
            try:
                key = item.label.lower()
                label_id = existing.get(key)
                if label_id is None:
                    new_label = self._mailbox.create_label(item.label)
                    label_id = new_label["id"]
                    existing[key] = label_id
                    created.append(item.label)
                # Atomic per group: add label + remove INBOX in one batchModify.
                result = self._mailbox.apply_label(
                    item.message_ids, label_id=label_id, archive=True
                )
                by_label[item.label] = result.modified
                archived += result.modified
                if result.failures:
                    failures.append({"label": item.label, "error": result.failures})
            except PermissionError:
                raise  # missing/expired modify scope -> web layer answers 403
            except Exception as exc:  # noqa: BLE001 - isolate a failing label group
                logger.exception("Smart archive failed for label %r", item.label)
                failures.append(
                    {"label": item.label, "message_ids": item.message_ids, "error": str(exc)}
                )

        logger.info(
            "Smart archive: %d archived across %d label(s), %d created, %d skipped, %d failed",
            archived, len(by_label), len(created), len(plan.skipped_unanalyzed), len(failures),
        )
        return ArchiveResult(
            archived=archived,
            labels_created=created,
            by_label=by_label,
            skipped_unanalyzed=len(plan.skipped_unanalyzed),
            failures=failures,
        )
