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
    """Reference the engine needs: id (cache + Gmail ops), sender (rules),
    an optional user ``override`` label, and subject (organization rules).

    ``override`` stays the 3rd positional for back-compat; ``subject`` is last.
    """

    id: str
    sender: str = ""
    override: str = ""
    subject: str = ""


class OrganizationRules:
    """User filing preferences (Settings → AI Organization). First-match-wins.

    Matches a sender substring, an exact/parent domain, or a subject keyword
    (comma-separated). The highest-priority filing source after an explicit
    override — the user's own preference beats the built-in rules and the AI.
    """

    def __init__(self, rules) -> None:
        # rules: iterable of (match, value, label)
        self._rules: list[tuple[str, str, str]] = []
        for match, value, label in rules or []:
            value = (value or "").strip().lower()
            label = (label or "").strip()
            if value and label:
                self._rules.append(((match or "domain").strip().lower(), value, label))

    def match(self, sender: str, subject: str = "") -> str | None:
        s = (sender or "").strip().lower()
        domain = s.split("@")[-1] if "@" in s else s
        subj = (subject or "").lower()
        for match, value, label in self._rules:
            if match == "sender" and value in s:
                return label
            if match == "domain" and (domain == value or domain.endswith("." + value)):
                return label
            if match == "subject":
                terms = [t.strip() for t in value.split(",") if t.strip()]
                if any(t in subj for t in terms):
                    return label
        return None


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

    def __init__(self, *, rules: RulesEngine, analysis_cache, learning=None, organization=None) -> None:
        self._rules = rules
        self._cache = analysis_cache
        self._learning = learning  # optional LearningStore (6B)
        self._org = organization   # optional OrganizationRules (user preferences)

    def decide_many(self, refs: list[EmailRef]) -> tuple[dict[str, FilingDecision], list[str]]:
        decisions: dict[str, FilingDecision] = {}
        undecided: list[str] = []
        # Look up cached analyses once for the ids not settled by a rule/learning.
        ai = self._cache.get_cached_many([r.id for r in refs])
        for ref in refs:
            # User correction wins over everything and is recorded so the
            # Learning Engine memorizes it (6B).
            if (ref.override or "").strip():
                decisions[ref.id] = FilingDecision(
                    label=ref.override.strip(), confidence=1.0, source="manual"
                )
                continue
            # User filing preferences (AI Organization) — the explicit user
            # intent, so it wins over the built-in rules / learning / AI.
            if self._org is not None:
                org_label = self._org.match(ref.sender, ref.subject)
                if org_label:
                    decisions[ref.id] = FilingDecision(
                        label=org_label, confidence=1.0, source="organization"
                    )
                    continue
            rule = self._rules.match(ref.sender)
            if rule is not None:
                decisions[ref.id] = FilingDecision(label=rule.label, confidence=1.0, source="rule")
                continue
            if self._learning is not None:
                learned = self._learning.learned_label(ref.sender)
                if learned is not None:
                    decisions[ref.id] = FilingDecision(
                        label=learned.label, confidence=learned.confidence, source="learned"
                    )
                    continue
            analysis = ai.get(ref.id)
            if analysis is not None:
                decisions[ref.id] = FilingDecision(
                    label=analysis.category, confidence=float(analysis.confidence), source="ai"
                )
                continue
            undecided.append(ref.id)
        return decisions, undecided
