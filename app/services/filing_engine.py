"""Filing decision engine — decide an email's label, cheapest source first.

Order of precedence (V2):
  1. Rules    — sender/domain glob rules. No LLM. High confidence.
  2. AI cache — the Phase 4 analysis category (only if already analyzed).
  3. (undecided) — needs an AI pass; the caller offers "Analyze & Continue".

The engine is the seam every smarter-filing feature plugs into. A learning
store (6B) will slot in between rules and the AI cache; confidence (6C),
hierarchical labels (6D) and multi-labels (6E) extend FilingDecision without
changing callers. Decisions carry a ``source`` + ``confidence`` so those phases
have what they need already.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from fnmatch import fnmatch

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FilingRule:
    """A glob pattern matched against the lowercased sender email -> label."""

    pattern: str
    label: str

    def matches(self, sender: str) -> bool:
        return fnmatch((sender or "").strip().lower(), self.pattern.strip().lower())


# Built-in rules. Easily extended: append here, or pass extra rules to
# RulesEngine (a future settings page / learning store can feed user rules in).
DEFAULT_RULES: list[FilingRule] = [
    FilingRule("*@amazon.*", "Shopping"),
    FilingRule("*@*.amazon.*", "Shopping"),
    FilingRule("*@paypal.com", "Finance"),
    FilingRule("*@*.paypal.com", "Finance"),
    FilingRule("*@edf.fr", "Utilities"),
    FilingRule("*@github.com", "Development"),
    FilingRule("*@*.github.com", "Development"),
    FilingRule("*@steam.*", "Gaming"),
    FilingRule("*@*steampowered.com", "Gaming"),
    FilingRule("*@universite-*", "Research"),
    FilingRule("*@*.edu", "Research"),
]


@dataclass(slots=True)
class EmailRef:
    """Minimal reference the engine needs: id (for cache + Gmail ops) + sender."""

    id: str
    sender: str = ""


@dataclass(slots=True)
class FilingDecision:
    label: str
    confidence: float
    source: str  # "rule" | "ai"


class RulesEngine:
    """First-match-wins glob rules over the sender. No LLM."""

    def __init__(self, rules: list[FilingRule] | None = None) -> None:
        self._rules = list(rules) if rules is not None else list(DEFAULT_RULES)

    def add_rule(self, pattern: str, label: str) -> None:
        self._rules.insert(0, FilingRule(pattern, label))  # user rules win

    def match(self, sender: str) -> FilingRule | None:
        s = (sender or "").strip().lower()
        if not s:
            return None
        for rule in self._rules:
            if rule.matches(s):
                return rule
        return None


class CacheReader:  # structural typing only; EmailAnalysisService satisfies this
    def get_cached_many(self, message_ids: list[str]) -> dict: ...  # pragma: no cover


class FilingResolver:
    """Combines rules + the AI analysis cache into per-email decisions.

    Never triggers an LLM call: rules use the sender, the AI branch only reads
    the cache. Undecided emails are returned so the caller can choose to analyze.
    """

    def __init__(self, *, rules: RulesEngine, analysis_cache) -> None:
        self._rules = rules
        self._cache = analysis_cache

    def decide_many(self, refs: list[EmailRef]) -> tuple[dict[str, FilingDecision], list[str]]:
        decisions: dict[str, FilingDecision] = {}
        undecided: list[str] = []
        # Look up cached analyses once for the ids not already settled by a rule.
        ai = self._cache.get_cached_many([r.id for r in refs])
        for ref in refs:
            rule = self._rules.match(ref.sender)
            if rule is not None:
                decisions[ref.id] = FilingDecision(label=rule.label, confidence=1.0, source="rule")
                continue
            analysis = ai.get(ref.id)
            if analysis is not None:
                decisions[ref.id] = FilingDecision(
                    label=analysis.category, confidence=float(analysis.confidence), source="ai"
                )
                continue
            undecided.append(ref.id)
        return decisions, undecided
