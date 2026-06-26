"""Smart Archive — the AI Filing engine (V2).

Files selected emails into a Gmail label, then archives them. The label for each
email is chosen by the :class:`FilingResolver`, cheapest source first:
rules (no LLM) → cached AI analysis → undecided (offer "Analyze & Continue").
This service itself NEVER calls the LLM — it only reads decisions and drives the
MailboxService.

Atomicity: each label group is filed with a single ``apply_label(archive=True)``
call (add label + remove INBOX in one Gmail batchModify), so an email is never
left labelled-but-not-archived — a failed group stays untouched and is reported.

The engine is reusable as-is for Auto Filing / Inbox Cleanup / Scheduled Cleanup
(pass the same EmailRefs); future hierarchical/multi-labels extend FilingDecision
without changing this service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.services.filing_engine import EmailRef, FilingResolver
from app.services.mailbox_service import MailboxService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ArchivePlanItem:
    label: str
    message_ids: list[str]
    source: str = "rule"  # dominant source for this group ("rule" | "ai")

    @property
    def count(self) -> int:
        return len(self.message_ids)


@dataclass(slots=True)
class ArchivePlan:
    items: list[ArchivePlanItem]
    total_selected: int
    undecided_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "items": [{"label": i.label, "count": i.count, "source": i.source} for i in self.items],
            "total_selected": self.total_selected,
            "decided": sum(i.count for i in self.items),
            "needs_analysis": len(self.undecided_ids),
            # Exact ids the UI should analyze for a one-click "Analyze & Continue".
            "unanalyzed_ids": list(self.undecided_ids),
        }


@dataclass(slots=True)
class ArchiveResult:
    archived: int
    labels_created: list[str]
    by_label: dict[str, int]
    needs_analysis: int
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "archived": self.archived,
            "labels_created": self.labels_created,
            "by_label": self.by_label,
            "needs_analysis": self.needs_analysis,
            "failed": len(self.failures),
            "failures": self.failures,
        }


class SmartArchiveService:
    def __init__(
        self, *, resolver: FilingResolver, mailbox: MailboxService, learning=None, clock=None
    ) -> None:
        self._resolver = resolver
        self._mailbox = mailbox
        self._learning = learning  # optional LearningStore (records on execute)
        from app.database.sqlite_manager import SQLiteManager

        self._now = clock or SQLiteManager.now_iso

    def _grouped(self, refs: list[EmailRef]):
        """Decide every ref and group ids by label. Returns
        (refs, decisions, undecided, groups) where groups[label] = {ids, sources}."""
        refs = _dedupe(refs)
        decisions, undecided = self._resolver.decide_many(refs)
        groups: dict[str, dict] = {}
        for ref in refs:
            decision = decisions.get(ref.id)
            if decision is None:
                continue
            label = (decision.label or "Other").strip() or "Other"
            decision.label = label
            group = groups.setdefault(label, {"ids": [], "sources": set()})
            group["ids"].append(ref.id)
            group["sources"].add(decision.source)
        return refs, decisions, undecided, groups

    def plan(self, refs: list[EmailRef]) -> ArchivePlan:
        """Group emails by their decided label (rules + learning + cache — no LLM)."""
        refs, _decisions, undecided, groups = self._grouped(refs)
        items = [
            ArchivePlanItem(
                label=label,
                message_ids=g["ids"],
                source=next(iter(g["sources"])) if len(g["sources"]) == 1 else "mixed",
            )
            for label, g in groups.items()
        ]
        items.sort(key=lambda i: (-i.count, i.label.lower()))
        return ArchivePlan(items=items, total_selected=len(refs), undecided_ids=undecided)

    def execute(self, refs: list[EmailRef]) -> ArchiveResult:
        """Create-if-needed + apply label + archive each group, then learn from it."""
        refs, decisions, undecided, groups = self._grouped(refs)
        if not groups:
            return ArchiveResult(
                archived=0, labels_created=[], by_label={}, needs_analysis=len(undecided)
            )
        sender_by_id = {r.id: r.sender for r in refs}

        # One labels listing; reuse existing (case-insensitive), create missing once.
        # A missing-scope error surfaces here, before any change is made.
        existing = {l["name"].lower(): l["id"] for l in self._mailbox.list_labels()}
        created: list[str] = []
        by_label: dict[str, int] = {}
        failures: list[dict] = []
        archived = 0

        for label, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]["ids"]), kv[0].lower())):
            ids = group["ids"]
            try:
                key = label.lower()
                label_id = existing.get(key)
                if label_id is None:
                    new_label = self._mailbox.create_label(label)
                    label_id = new_label["id"]
                    existing[key] = label_id
                    created.append(label)
                # Atomic per group: add label + remove INBOX in one batchModify.
                result = self._mailbox.apply_label(ids, label_id=label_id, archive=True)
                by_label[label] = result.modified
                archived += result.modified
                if result.failures:
                    failures.append({"label": label, "error": result.failures})
                else:
                    self._learn(ids, decisions, sender_by_id)
            except PermissionError:
                raise  # missing/expired modify scope -> web layer answers 403
            except Exception as exc:  # noqa: BLE001 - isolate a failing label group
                logger.exception("Smart archive failed for label %r", label)
                failures.append({"label": label, "message_ids": ids, "error": str(exc)})

        logger.info(
            "Smart archive: %d archived across %d label(s), %d created, %d need analysis, %d failed",
            archived, len(by_label), len(created), len(undecided), len(failures),
        )
        return ArchiveResult(
            archived=archived,
            labels_created=created,
            by_label=by_label,
            needs_analysis=len(undecided),
            failures=failures,
        )

    def _learn(self, ids: list[str], decisions: dict, sender_by_id: dict) -> None:
        """Record each successfully-filed email so the engine learns the habit."""
        if self._learning is None:
            return
        when = self._now()
        for mid in ids:
            decision = decisions.get(mid)
            if decision is None:
                continue
            try:
                self._learning.record(
                    message_id=mid,
                    sender=sender_by_id.get(mid, ""),
                    label=decision.label,
                    source=decision.source,
                    confidence=decision.confidence,
                    when=when,
                )
            except Exception:  # noqa: BLE001 - learning must never break filing
                logger.exception("Failed to record filing history for %s", mid)


def _dedupe(refs: list[EmailRef]) -> list[EmailRef]:
    seen: set[str] = set()
    out: list[EmailRef] = []
    for ref in refs:
        if ref.id and ref.id not in seen:
            seen.add(ref.id)
            out.append(ref)
    return out
