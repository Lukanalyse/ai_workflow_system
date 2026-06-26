"""Learning Engine — learn the user's filing habits, decide without the LLM.

Every Smart Archive filing is recorded (sender, domain, label). Next time the
same sender (or, failing that, the same domain) shows up, the learned label is
returned with a confidence that grows as the evidence does — so a recurring
sender/newsletter/shop/bank gets filed straight away, no LLM call.

The store is backed by SQLite today (``filing_history``) but only depends on a
few count/record methods, so it can be swapped later. It slots into the
FilingResolver between rules and the AI cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.database.sqlite_manager import SQLiteManager

logger = logging.getLogger(__name__)

# How much evidence is needed before a learned label is trusted.
SENDER_MIN = 1   # one prior filing from this exact sender is enough
DOMAIN_MIN = 2   # a domain needs a couple of consistent filings to generalize


def _sender_confidence(dominant: int) -> float:
    return min(0.95, 0.6 + 0.1 * dominant)


def _domain_confidence(dominant: int) -> float:
    return min(0.90, 0.5 + 0.08 * dominant)


def _domain_of(sender: str) -> str:
    s = (sender or "").strip().lower()
    return s.split("@", 1)[1] if "@" in s else ""


def _dominant(counts: dict[str, int]) -> tuple[str, int] | None:
    """Return (label, count) for the strict majority label, or None on a tie/empty."""
    if not counts:
        return None
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_label, top_n = ordered[0]
    if len(ordered) > 1 and ordered[1][1] == top_n:
        return None  # ambiguous — don't guess
    return top_label, top_n


@dataclass(slots=True)
class LearnedLabel:
    label: str
    confidence: float


class LearningStore:
    def __init__(self, sqlite: SQLiteManager) -> None:
        self._db = sqlite

    def record(
        self, *, message_id: str, sender: str, label: str, source: str, confidence: float, when: str
    ) -> None:
        sender = (sender or "").strip().lower()
        self._db.record_filing(
            message_id=message_id,
            sender=sender,
            domain=_domain_of(sender),
            label=label,
            source=source,
            confidence=confidence,
            created_at=when,
        )

    def learned_label(self, sender: str) -> LearnedLabel | None:
        """Best learned label for a sender: exact sender first, then its domain."""
        sender = (sender or "").strip().lower()
        if not sender:
            return None
        dom = _dominant(self._db.filing_label_counts_for_sender(sender))
        if dom and dom[1] >= SENDER_MIN:
            return LearnedLabel(label=dom[0], confidence=_sender_confidence(dom[1]))
        domain = _domain_of(sender)
        dom = _dominant(self._db.filing_label_counts_for_domain(domain))
        if dom and dom[1] >= DOMAIN_MIN:
            return LearnedLabel(label=dom[0], confidence=_domain_confidence(dom[1]))
        return None
