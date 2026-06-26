"""Phase 5 tests: Smart Archive (AI filing) — cache only, never the LLM.

Covers the spec cases: single, multiple, existing-label reuse, auto-create,
successful archive, Gmail error isolation + clean (atomic) per-group behavior,
and a hard guarantee that no LLM call is ever made.
"""

from __future__ import annotations

import pytest

from app.llm.email_analysis import EmailAnalysis
from app.services.mailbox_service import ActionResult
from app.services.smart_archive_service import SmartArchiveService


def _a(category: str) -> EmailAnalysis:
    return EmailAnalysis(
        summary="s", category=category, priority="Medium",
        needs_reply=False, action_recommended="Archive", confidence=0.8,
    )


class FakeCache:
    """Read-only analysis cache. Raises if anyone tries to (re)analyze."""

    def __init__(self, mapping: dict[str, EmailAnalysis]) -> None:
        self._map = mapping

    def get_cached_many(self, ids):
        return {i: self._map[i] for i in ids if i in self._map}

    # If Smart Archive ever called the analyze path, these would blow up the test.
    def analyze(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("Smart Archive must not trigger an LLM analysis.")


class FakeMailbox:
    def __init__(self, *, existing=None, fail_labels=None) -> None:
        self.labels = list(existing or [])  # [{id,name,type}]
        self._fail = set(fail_labels or [])  # label names whose apply fails
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
        # Find the label name for failure simulation.
        name = next((l["name"] for l in self.labels if l["id"] == label_id), label_id)
        if name in self._fail:
            return ActionResult(
                action="apply_label", requested=len(ids), modified=0,
                failures=[{"message_ids": ids, "error": "Gmail 500"}],
            )
        self.applied.append({"ids": list(ids), "label_id": label_id, "archive": archive})
        return ActionResult(action="apply_label", requested=len(ids), modified=len(ids))


def _svc(cache_map, mailbox):
    return SmartArchiveService(analysis_cache=FakeCache(cache_map), mailbox=mailbox)


def test_single_email_creates_label_and_archives():
    mb = FakeMailbox(existing=[])
    res = _svc({"m1": _a("Research")}, mb).execute(["m1"])
    assert res.archived == 1 and res.labels_created == ["Research"]
    assert mb.applied[0]["archive"] is True
    assert mb.list_calls == 1  # labels listed once


def test_multiple_groups_reuse_existing_and_create_missing():
    mb = FakeMailbox(existing=[{"id": "L_fin", "name": "Finance", "type": "user"}])
    cache = {"a": _a("Finance"), "b": _a("Finance"), "c": _a("Research")}
    res = _svc(cache, mb).execute(["a", "b", "c"])
    assert res.archived == 3
    assert res.labels_created == ["Research"]  # Finance reused, Research created
    assert res.by_label == {"Finance": 2, "Research": 1}


def test_existing_label_case_insensitive_not_recreated():
    mb = FakeMailbox(existing=[{"id": "L_fin", "name": "finance", "type": "user"}])
    res = _svc({"a": _a("Finance")}, mb).execute(["a"])
    assert res.labels_created == [] and res.archived == 1
    assert mb.created == []


def test_unanalyzed_emails_are_skipped_not_analyzed():
    mb = FakeMailbox(existing=[])
    # "b" has no cached analysis -> skipped, never sent to an LLM.
    res = _svc({"a": _a("Work")}, mb).execute(["a", "b"])
    assert res.archived == 1 and res.skipped_unanalyzed == 1


def test_plan_groups_and_counts_without_touching_gmail():
    mb = FakeMailbox(existing=[])
    plan = _svc({"a": _a("Shopping"), "b": _a("Shopping"), "c": _a("Finance")}, mb).plan(["a", "b", "c", "z"])
    d = plan.as_dict()
    assert d["analyzed"] == 3 and d["skipped_unanalyzed"] == 1 and d["total_selected"] == 4
    assert {i["label"]: i["count"] for i in d["items"]} == {"Shopping": 2, "Finance": 1}
    assert mb.list_calls == 0  # preview never lists/creates labels


def test_failed_group_is_isolated_and_atomic():
    # Finance fails (label applied+archive is one atomic call -> nothing changes
    # for that group); Research still succeeds.
    mb = FakeMailbox(existing=[], fail_labels={"Finance"})
    cache = {"a": _a("Finance"), "b": _a("Research")}
    res = _svc(cache, mb).execute(["a", "b"])
    assert res.archived == 1  # only Research
    assert res.by_label.get("Research") == 1
    assert len(res.failures) == 1
    # Finance group never recorded an applied (atomic): not archived, not partial.
    assert all(a["label_id"] != "L_Finance" for a in mb.applied)


def test_no_items_when_nothing_analyzed():
    mb = FakeMailbox(existing=[])
    res = _svc({}, mb).execute(["a", "b"])
    assert res.archived == 0 and res.skipped_unanalyzed == 2 and mb.list_calls == 0


def test_missing_scope_propagates_permission_error():
    class _Denied(FakeMailbox):
        def list_labels(self):
            raise PermissionError("reconnect Gmail")

    with pytest.raises(PermissionError):
        _svc({"a": _a("Work")}, _Denied()).execute(["a"])
