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
    def __init__(self, *, resolver: FilingResolver, mailbox: MailboxService) -> None:
        self._resolver = resolver
        self._mailbox = mailbox

    def plan(self, refs: list[EmailRef]) -> ArchivePlan:
        """Group emails by their decided label (rules + cache only — no LLM)."""
        refs = _dedupe(refs)
        decisions, undecided = self._resolver.decide_many(refs)
        groups: dict[str, list[str]] = {}
        sources: dict[str, set[str]] = {}
        for ref in refs:
            decision = decisions.get(ref.id)
            if decision is None:
                continue
            label = (decision.label or "Other").strip() or "Other"
            groups.setdefault(label, []).append(ref.id)
            sources.setdefault(label, set()).add(decision.source)
        items = [
            ArchivePlanItem(
                label=label,
                message_ids=mids,
                source="rule" if sources.get(label) == {"rule"} else
                ("ai" if sources.get(label) == {"ai"} else "mixed"),
            )
            for label, mids in groups.items()
        ]
        items.sort(key=lambda i: (-i.count, i.label.lower()))
        return ArchivePlan(items=items, total_selected=len(refs), undecided_ids=undecided)

    def execute(self, refs: list[EmailRef]) -> ArchiveResult:
        """Create-if-needed + apply label + archive each group."""
        plan = self.plan(refs)
        if not plan.items:
            return ArchiveResult(
                archived=0, labels_created=[], by_label={},
                needs_analysis=len(plan.undecided_ids),
            )

        # One labels listing; reuse existing (case-insensitive), create missing once.
        # A missing-scope error surfaces here, before any change is made.
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
            "Smart archive: %d archived across %d label(s), %d created, %d need analysis, %d failed",
            archived, len(by_label), len(created), len(plan.undecided_ids), len(failures),
        )
        return ArchiveResult(
            archived=archived,
            labels_created=created,
            by_label=by_label,
            needs_analysis=len(plan.undecided_ids),
            failures=failures,
        )


def _dedupe(refs: list[EmailRef]) -> list[EmailRef]:
    seen: set[str] = set()
    out: list[EmailRef] = []
    for ref in refs:
        if ref.id and ref.id not in seen:
            seen.add(ref.id)
            out.append(ref)
    return out
