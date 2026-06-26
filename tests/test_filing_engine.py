"""Phase 6A tests: the rules engine + filing resolver (no LLM)."""

from __future__ import annotations

from app.llm.email_analysis import EmailAnalysis
from app.services.filing_engine import (
    DEFAULT_RULES,
    EmailRef,
    FilingRule,
    FilingResolver,
    RulesEngine,
)


def test_rule_glob_matches_domain_variants():
    r = FilingRule("*@amazon.*", "Shopping")
    assert r.matches("orders@amazon.fr")
    assert r.matches("ORDERS@Amazon.COM")  # case-insensitive
    assert not r.matches("hi@notamazon.com")


def test_default_rules_cover_spec_examples():
    re = RulesEngine()
    cases = {
        "deal@amazon.com": "Shopping",
        "service@paypal.com": "Finance",
        "facture@edf.fr": "Utilities",
        "noreply@github.com": "Development",
        "news@steam.press": "Gaming",
        "prof@universite-lyon.fr": "Research",
    }
    for sender, label in cases.items():
        m = re.match(sender)
        assert m is not None and m.label == label, sender


def test_no_match_returns_none_and_empty_sender_safe():
    re = RulesEngine()
    assert re.match("someone@randomcompany.io") is None
    assert re.match("") is None


def test_added_rule_takes_priority():
    re = RulesEngine([FilingRule("*@amazon.*", "Shopping")])
    re.add_rule("*@amazon.fr", "FR-Shopping")  # inserted first -> wins
    assert re.match("x@amazon.fr").label == "FR-Shopping"


class _Cache:
    def __init__(self, mapping):
        self._m = mapping

    def get_cached_many(self, ids):
        return {i: self._m[i] for i in ids if i in self._m}


def _an(cat, conf=0.7):
    return EmailAnalysis(summary="s", category=cat, priority="Low",
                         needs_reply=False, action_recommended="Archive", confidence=conf)


def test_resolver_precedence_rule_then_ai_then_undecided():
    rules = RulesEngine([FilingRule("*@paypal.com", "Finance")])
    cache = _Cache({"b": _an("Work", 0.9)})
    resolver = FilingResolver(rules=rules, analysis_cache=cache)
    refs = [
        EmailRef("a", "pay@paypal.com"),  # rule
        EmailRef("b", "boss@corp.com"),   # ai cache
        EmailRef("c", "x@unknown.io"),    # undecided
    ]
    decisions, undecided = resolver.decide_many(refs)
    assert decisions["a"].source == "rule" and decisions["a"].label == "Finance"
    assert decisions["a"].confidence == 1.0
    assert decisions["b"].source == "ai" and decisions["b"].label == "Work"
    assert decisions["b"].confidence == 0.9
    assert undecided == ["c"]


def test_resolver_reads_cache_once_and_never_analyzes():
    calls = {"n": 0}

    class _CountingCache:
        def get_cached_many(self, ids):
            calls["n"] += 1
            return {}

    resolver = FilingResolver(rules=RulesEngine([]), analysis_cache=_CountingCache())
    _, undecided = resolver.decide_many([EmailRef("a", "x@y.z"), EmailRef("b", "p@q.r")])
    assert undecided == ["a", "b"] and calls["n"] == 1


def test_default_rules_are_nonempty():
    assert len(DEFAULT_RULES) >= 6
