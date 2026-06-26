"""Phase 5/6A tests: Smart Archive (AI filing) — rules + cache, never the LLM.

Covers: single, multiple, existing-label reuse, auto-create, success, Gmail
error isolation + atomic per-group behavior, rule-based filing without any AI,
and a hard guarantee that no LLM call is ever made.
"""

from __future__ import annotations

import pytest

from app.llm.email_analysis import EmailAnalysis
from app.services.filing_engine import EmailRef, FilingResolver, FilingRule, RulesEngine
from app.services.mailbox_service import ActionResult
from app.services.smart_archive_service import SmartArchiveService


def _a(category: str) -> EmailAnalysis:
    return EmailAnalysis(
        summary="s", category=category, priority="Medium",
        needs_reply=False, action_recommended="Archive", confidence=0.8,
    )


def _refs(*specs):
    """specs are ids or (id, sender) tuples."""
    out = []
    for s in specs:
        out.append(EmailRef(*s) if isinstance(s, tuple) else EmailRef(s))
    return out


class FakeCache:
    """Read-only analysis cache. Raises if anyone tries to (re)analyze."""

    def __init__(self, mapping: dict[str, EmailAnalysis]) -> None:
        self._map = mapping

    def get_cached_many(self, ids):
        return {i: self._map[i] for i in ids if i in self._map}

    def analyze(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("Smart Archive must not trigger an LLM analysis.")


class FakeMailbox:
    def __init__(self, *, existing=None, fail_labels=None) -> None:
        self.labels = list(existing or [])
        self._fail = set(fail_labels or [])
        self.created: list[str] = []
        self.applied: list[dict] = []
        self.list_calls = 0

    def list_labels(self):
        self.list_calls += 1
        return list(self.labels)

    def create_label(self, name):
        label = {"id": f"L_{name}", "name": name, "type": "user"}
        self.labels.append(label)
        self.created.append(name)
        return label

    def apply_label(self, ids, *, label_id, archive=False):
        name = next((l["name"] for l in self.labels if l["id"] == label_id), label_id)
        if name in self._fail:
            return ActionResult(
                action="apply_label", requested=len(ids), modified=0,
                failures=[{"message_ids": ids, "error": "Gmail 500"}],
            )
        self.applied.append({"ids": list(ids), "label_id": label_id, "archive": archive})
        return ActionResult(action="apply_label", requested=len(ids), modified=len(ids))


def _svc(cache_map, mailbox, rules=None):
    resolver = FilingResolver(
        rules=RulesEngine(rules if rules is not None else []),  # no default rules unless asked
        analysis_cache=FakeCache(cache_map),
    )
    return SmartArchiveService(resolver=resolver, mailbox=mailbox)


def test_single_email_creates_label_and_archives():
    mb = FakeMailbox(existing=[])
    res = _svc({"m1": _a("Research")}, mb).execute(_refs("m1"))
    assert res.archived == 1 and res.labels_created == ["Research"]
    assert mb.applied[0]["archive"] is True
    assert mb.list_calls == 1


def test_multiple_groups_reuse_existing_and_create_missing():
    mb = FakeMailbox(existing=[{"id": "L_fin", "name": "Finance", "type": "user"}])
    cache = {"a": _a("Finance"), "b": _a("Finance"), "c": _a("Research")}
    res = _svc(cache, mb).execute(_refs("a", "b", "c"))
    assert res.archived == 3
    assert res.labels_created == ["Research"]
    assert res.by_label == {"Finance": 2, "Research": 1}


def test_existing_label_case_insensitive_not_recreated():
    mb = FakeMailbox(existing=[{"id": "L_fin", "name": "finance", "type": "user"}])
    res = _svc({"a": _a("Finance")}, mb).execute(_refs("a"))
    assert res.labels_created == [] and res.archived == 1 and mb.created == []


def test_unanalyzed_emails_need_analysis_not_analyzed_here():
    mb = FakeMailbox(existing=[])
    res = _svc({"a": _a("Work")}, mb).execute(_refs("a", "b"))
    assert res.archived == 1 and res.needs_analysis == 1


def test_rule_files_without_any_ai_analysis():
    # No cached analysis at all; a sender rule decides the label -> no LLM needed.
    mb = FakeMailbox(existing=[])
    rules = [FilingRule("*@amazon.*", "Shopping")]
    res = _svc({}, mb, rules=rules).execute(_refs(("m1", "deals@amazon.fr")))
    assert res.archived == 1 and res.by_label == {"Shopping": 1}
    assert res.needs_analysis == 0


def test_rule_takes_precedence_over_ai_cache():
    mb = FakeMailbox(existing=[])
    rules = [FilingRule("*@paypal.com", "Finance")]
    # AI says "Other" but the rule wins -> Finance.
    res = _svc({"m1": _a("Other")}, mb, rules=rules).plan(_refs(("m1", "pay@paypal.com")))
    assert res.items[0].label == "Finance" and res.items[0].source == "rule"


def test_plan_groups_and_counts_without_touching_gmail():
    mb = FakeMailbox(existing=[])
    plan = _svc({"a": _a("Shopping"), "b": _a("Shopping"), "c": _a("Finance")}, mb).plan(
        _refs("a", "b", "c", "z")
    )
    d = plan.as_dict()
    assert d["decided"] == 3 and d["needs_analysis"] == 1 and d["total_selected"] == 4
    assert {i["label"]: i["count"] for i in d["items"]} == {"Shopping": 2, "Finance": 1}
    assert d["unanalyzed_ids"] == ["z"]
    assert mb.list_calls == 0


def test_failed_group_is_isolated_and_atomic():
    mb = FakeMailbox(existing=[], fail_labels={"Finance"})
    cache = {"a": _a("Finance"), "b": _a("Research")}
    res = _svc(cache, mb).execute(_refs("a", "b"))
    assert res.archived == 1  # only Research
    assert res.by_label.get("Research") == 1
    assert len(res.failures) == 1
    assert all(a["label_id"] != "L_Finance" for a in mb.applied)


def test_no_items_when_nothing_decided():
    mb = FakeMailbox(existing=[])
    res = _svc({}, mb).execute(_refs("a", "b"))
    assert res.archived == 0 and res.needs_analysis == 2 and mb.list_calls == 0


def test_override_beats_rule_and_is_source_manual():
    mb = FakeMailbox(existing=[])
    rules = [FilingRule("*@amazon.*", "Shopping")]
    # User corrects the suggestion to "Gifts" via an override on the ref.
    res = _svc({}, mb, rules=rules).plan([EmailRef("m1", "deals@amazon.fr", "Gifts")])
    assert res.items[0].label == "Gifts" and res.items[0].source == "manual"


def test_plan_exposes_confidence_and_ids():
    mb = FakeMailbox(existing=[])
    d = _svc({"a": _a("Finance")}, mb).plan(_refs("a")).as_dict()
    item = d["items"][0]
    assert item["label"] == "Finance" and item["count"] == 1
    assert 0.0 <= item["confidence"] <= 1.0
    assert item["message_ids"] == ["a"] and "source" in item


def test_missing_scope_propagates_permission_error():
    class _Denied(FakeMailbox):
        def list_labels(self):
            raise PermissionError("reconnect Gmail")

    with pytest.raises(PermissionError):
        _svc({"a": _a("Work")}, _Denied()).execute(_refs("a"))
