"""Phase 6A tests: the rules engine + filing resolver (no LLM)."""

from __future__ import annotations

from app.llm.email_analysis import EmailAnalysis
from app.services.filing_engine import (
    DEFAULT_RULES,
    EmailRef,
    FilingRule,
    FilingResolver,
    OrganizationRules,
    RulesEngine,
)


# --- AI Organization (user filing preferences) -------------------------------
def test_org_rules_match_sender_domain_subject():
    org = OrganizationRules([
        ("domain", "google.com", "Google"),
        ("sender", "amazon", "Shopping"),
        ("subject", "invoice, facture", "Finance"),
    ])
    assert org.match("noreply@accounts.google.com") == "Google"   # parent domain
    assert org.match("x@google.com") == "Google"                  # exact domain
    assert org.match("ship@amazon.fr") == "Shopping"              # sender substring
    assert org.match("a@b.com", "Your invoice is ready") == "Finance"
    assert org.match("a@b.com", "Votre facture") == "Finance"
    assert org.match("a@b.com", "hello") is None


def test_org_domain_does_not_loosely_match():
    org = OrganizationRules([("domain", "google.com", "Google")])
    assert org.match("x@notgoogle.com.evil.test") is None  # not a parent domain


def test_resolver_organization_beats_rules_and_ai():
    rules = RulesEngine([FilingRule("*@github.com", "Development")])
    cache = _Cache({"c": _an("Work", 0.9)})
    org = OrganizationRules([
        ("domain", "github.com", "Code"),     # overrides the built-in Development
        ("subject", "invoice", "Finance"),
    ])
    resolver = FilingResolver(rules=rules, analysis_cache=cache, organization=org)
    refs = [
        EmailRef("a", "noreply@github.com"),                 # org domain beats rule
        EmailRef("b", "x@unknown.io", subject="Your invoice"),  # org subject
        EmailRef("c", "boss@corp.com"),                      # falls through to AI cache
    ]
    decisions, undecided = resolver.decide_many(refs)
    assert decisions["a"].source == "organization" and decisions["a"].label == "Code"
    assert decisions["b"].source == "organization" and decisions["b"].label == "Finance"
    assert decisions["c"].source == "ai" and decisions["c"].label == "Work"
    assert undecided == []


def test_override_still_beats_organization():
    org = OrganizationRules([("domain", "github.com", "Code")])
    resolver = FilingResolver(rules=RulesEngine([]), analysis_cache=_Cache({}), organization=org)
    decisions, _ = resolver.decide_many([EmailRef("a", "x@github.com", override="Pinned")])
    assert decisions["a"].source == "manual" and decisions["a"].label == "Pinned"


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
