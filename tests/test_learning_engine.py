"""Phase 6B tests: Learning Engine + history (no LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.database.sqlite_manager import SQLiteManager
from app.llm.email_analysis import EmailAnalysis
from app.services.filing_engine import EmailRef, FilingResolver, FilingRule, RulesEngine
from app.services.learning_engine import LearningStore
from app.services.mailbox_service import ActionResult
from app.services.smart_archive_service import SmartArchiveService


@pytest.fixture()
def db(tmp_path: Path) -> SQLiteManager:
    return SQLiteManager(tmp_path / "h.db")


def _rec(ls: LearningStore, db: SQLiteManager, sender: str, label: str, n: int = 1):
    for i in range(n):
        ls.record(message_id=f"{sender}-{label}-{i}", sender=sender, label=label,
                  source="ai", confidence=0.8, when=db.now_iso())


def test_learns_sender_with_growing_confidence(db):
    ls = LearningStore(db)
    assert ls.learned_label("marie@acme.com") is None
    _rec(ls, db, "marie@acme.com", "Finance", 1)
    a = ls.learned_label("MARIE@acme.com")  # case-insensitive
    assert a.label == "Finance" and a.confidence == pytest.approx(0.7)
    _rec(ls, db, "marie@acme.com", "Finance", 2)  # total 3
    assert ls.learned_label("marie@acme.com").confidence == pytest.approx(0.9)


def test_domain_fallback_requires_two(db):
    ls = LearningStore(db)
    _rec(ls, db, "a@shop.com", "Shopping", 1)
    # New sender, same domain, only 1 domain sample -> not yet generalized.
    assert ls.learned_label("b@shop.com") is None
    _rec(ls, db, "c@shop.com", "Shopping", 1)  # domain now has 2
    learned = ls.learned_label("d@shop.com")
    assert learned.label == "Shopping" and learned.confidence == pytest.approx(0.66)


def test_tie_is_not_learned(db):
    ls = LearningStore(db)
    _rec(ls, db, "x@y.com", "Finance", 1)
    _rec(ls, db, "x@y.com", "Work", 1)  # 1 vs 1 -> ambiguous
    assert ls.learned_label("x@y.com") is None


def test_empty_sender_safe(db):
    assert LearningStore(db).learned_label("") is None


def test_resolver_learned_beats_ai(db):
    ls = LearningStore(db)
    _rec(ls, db, "boss@corp.com", "Work", 2)

    class _Cache:
        def get_cached_many(self, ids):
            return {"x": EmailAnalysis("s", "Other", "Low", False, "Archive", 0.9)}

    res = FilingResolver(rules=RulesEngine([]), analysis_cache=_Cache(), learning=ls)
    d, _ = res.decide_many([EmailRef("x", "boss@corp.com")])
    assert d["x"].source == "learned" and d["x"].label == "Work"


def test_resolver_rule_beats_learned(db):
    ls = LearningStore(db)
    _rec(ls, db, "x@amazon.fr", "Finance", 3)  # learned would say Finance

    class _Cache:
        def get_cached_many(self, ids):
            return {}

    res = FilingResolver(
        rules=RulesEngine([FilingRule("*@amazon.*", "Shopping")]),
        analysis_cache=_Cache(), learning=ls,
    )
    d, _ = res.decide_many([EmailRef("x", "x@amazon.fr")])
    assert d["x"].source == "rule" and d["x"].label == "Shopping"


# --- End-to-end: file once via AI, then learn for next time -------------------
class _FakeMailbox:
    def __init__(self):
        self.labels = []
        self.applied = []

    def list_labels(self):
        return list(self.labels)

    def create_label(self, name):
        lab = {"id": f"L_{name}", "name": name, "type": "user"}
        self.labels.append(lab)
        return lab

    def apply_label(self, ids, *, label_id, archive=False, remove_labels=None):
        self.applied.append({"ids": list(ids), "label_id": label_id})
        return ActionResult(action="apply_label", requested=len(ids), modified=len(ids))


def test_execute_records_history_then_next_time_learned(db):
    ls = LearningStore(db)

    class _Cache:
        # Only m1 has an analysis; m2 (same sender) has none.
        def get_cached_many(self, ids):
            out = {}
            if "m1" in ids:
                out["m1"] = EmailAnalysis("s", "Finance", "High", True, "Reply", 0.9)
            return out

    resolver = FilingResolver(rules=RulesEngine([]), analysis_cache=_Cache(), learning=ls)
    svc = SmartArchiveService(resolver=resolver, mailbox=_FakeMailbox(), learning=ls)

    # First: m1 decided by AI -> filed -> recorded.
    res1 = svc.execute([EmailRef("m1", "marie@acme.com")])
    assert res1.archived == 1 and res1.by_label == {"Finance": 1}
    assert db.recent_filing_history()[0]["label"] == "Finance"

    # Next time: m2 from the same sender has NO analysis, but is now LEARNED.
    plan = svc.plan([EmailRef("m2", "marie@acme.com")])
    assert plan.as_dict()["needs_analysis"] == 0
    assert plan.items[0].label == "Finance" and plan.items[0].source == "learned"


def test_user_override_is_memorized_and_learned(db):
    # User corrects a suggestion -> "manual" is recorded -> next time it's learned.
    ls = LearningStore(db)

    class _Cache:
        def get_cached_many(self, ids):
            return {"m1": EmailAnalysis("s", "Other", "Low", False, "Archive", 0.5)}

    resolver = FilingResolver(rules=RulesEngine([]), analysis_cache=_Cache(), learning=ls)
    svc = SmartArchiveService(resolver=resolver, mailbox=_FakeMailbox(), learning=ls)

    # AI said "Other" but the user files it as "Banking" (override).
    res = svc.execute([EmailRef("m1", "advisor@bank.com", "Banking")])
    assert res.by_label == {"Banking": 1}
    row = db.recent_filing_history()[0]
    assert row["label"] == "Banking" and row["source"] == "manual"

    # Next email from the same sender is now learned as "Banking".
    plan = svc.plan([EmailRef("m2", "advisor@bank.com")])
    assert plan.items[0].label == "Banking" and plan.items[0].source == "learned"


def test_no_learning_store_means_no_recording(db):
    # SmartArchive without a learning store must still work (records nothing).
    class _Cache:
        def get_cached_many(self, ids):
            return {"m1": EmailAnalysis("s", "Work", "Low", False, "Archive", 0.7)}

    svc = SmartArchiveService(
        resolver=FilingResolver(rules=RulesEngine([]), analysis_cache=_Cache()),
        mailbox=_FakeMailbox(),
    )
    res = svc.execute([EmailRef("m1", "a@b.c")])
    assert res.archived == 1 and db.recent_filing_history() == []
