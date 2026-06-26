"""Phase 4 tests: the AI understanding layer (parse, prompt, cache, service).

No real LLM is called — a fake stands in — so we can assert the contract:
normalization across categories/languages/lengths, no-subject and attachment
handling, and the analyze-once cache guarantee.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database.sqlite_manager import SQLiteManager
from app.llm.email_analysis import (
    ACTIONS,
    CATEGORIES,
    PRIORITIES,
    EmailAnalysis,
    build_analyze_prompts,
    parse_analysis,
)
from app.providers.base import EmailMessage
from app.services.email_analysis_service import EmailAnalysisService, SQLiteAnalysisCache


def _email(**kw) -> EmailMessage:
    base = dict(
        id="m1",
        thread_id="t1",
        subject="Invoice #2048",
        sender_email="billing@acme.com",
        sender_name="ACME Billing",
        internet_message_id="<x@acme>",
        received_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        snippet="Your invoice is due Friday.",
        body_text="Your invoice #2048 is due Friday. Please confirm payment.",
    )
    base.update(kw)
    return EmailMessage(**base)


# --- parse_analysis normalization --------------------------------------------
@pytest.mark.parametrize(
    "raw_cat,expected",
    [("finance", "Finance"), ("WORK", "Work"), ("travel", "Travel"), ("unknown-x", "Other")],
)
def test_parse_normalizes_category(raw_cat, expected):
    txt = f'{{"summary":"s","category":"{raw_cat}","priority":"low","needs_reply":false,"action_recommended":"archive","confidence":0.4}}'
    assert parse_analysis(txt).category == expected


def test_parse_normalizes_priority_action_and_bool():
    txt = '{"summary":"s","category":"Work","priority":"CRITICAL","needs_reply":"yes","action_recommended":"Read Later","confidence":2}'
    a = parse_analysis(txt)
    assert a.priority == "Critical"
    assert a.action_recommended == "Read Later"
    assert a.needs_reply is True
    assert a.confidence == 1.0  # clamped into [0,1]


def test_parse_unknown_priority_action_fall_back():
    txt = '{"summary":"s","category":"Work","priority":"urgent","needs_reply":0,"action_recommended":"delete","confidence":-1}'
    a = parse_analysis(txt)
    assert a.priority == "Medium" and a.action_recommended == "Other"
    assert a.needs_reply is False and a.confidence == 0.0


def test_parse_tolerates_prose_and_fences():
    txt = "Sure, here you go:\n```json\n{\"summary\":\"hi\",\"category\":\"Spam\",\"priority\":\"Low\",\"needs_reply\":false,\"action_recommended\":\"Ignore\",\"confidence\":0.2}\n```"
    a = parse_analysis(txt, model="m")
    assert a.category == "Spam" and a.model == "m"


def test_parse_preserves_non_english_summary():
    txt = '{"summary":"La facture est due vendredi. Merci de confirmer le paiement.","category":"Finance","priority":"High","needs_reply":true,"action_recommended":"Reply","confidence":0.8}'
    a = parse_analysis(txt)
    assert "facture" in a.summary and a.category == "Finance"


def test_parse_truncates_very_long_summary():
    long_summary = "x" * 5000
    txt = f'{{"summary":"{long_summary}","category":"Other","priority":"Low","needs_reply":false,"action_recommended":"Other","confidence":0.5}}'
    a = parse_analysis(txt)
    assert len(a.summary) <= 601 and a.summary.endswith("…")


def test_parse_raises_without_json():
    with pytest.raises(ValueError):
        parse_analysis("no json here")


# --- prompt building ----------------------------------------------------------
def test_prompt_lists_full_taxonomy():
    _, user = build_analyze_prompts(subject="Hi", sender="a@b.c", attachments=None, body="body")
    for value in CATEGORIES + PRIORITIES + ACTIONS:
        assert value in user


def test_prompt_handles_no_subject_and_attachments():
    _, user = build_analyze_prompts(
        subject="", sender="a@b.c", attachments=["contract.pdf", "photo.png"], body="..."
    )
    assert "(no subject)" in user
    assert "contract.pdf" in user and "photo.png" in user


def test_prompt_handles_short_and_long_bodies():
    _, short = build_analyze_prompts(subject="s", sender="a@b.c", attachments=None, body="ok")
    assert short.rstrip().endswith("ok")
    big = "word " * 4000
    _, long_user = build_analyze_prompts(subject="s", sender="a@b.c", attachments=None, body=big)
    assert big in long_user  # builder passes the (already-trimmed) body through verbatim


# --- service + cache (analyze-once guarantee) --------------------------------
class _FakeLLM:
    def __init__(self, analysis: EmailAnalysis) -> None:
        self._analysis = analysis
        self.calls = 0

    def analyze_email(self, email, *, run_id=None) -> EmailAnalysis:
        self.calls += 1
        return replace(self._analysis)


class _MemCache:
    def __init__(self) -> None:
        self.store: dict[str, EmailAnalysis] = {}

    def get(self, mid):
        return self.store.get(mid)

    def get_many(self, ids):
        return {i: self.store[i] for i in ids if i in self.store}

    def set(self, mid, analysis):
        self.store[mid] = analysis


def _analysis() -> EmailAnalysis:
    return EmailAnalysis(
        summary="Invoice due Friday.", category="Finance", priority="High",
        needs_reply=True, action_recommended="Reply", confidence=0.9,
    )


def test_service_caches_after_first_analysis():
    llm = _FakeLLM(_analysis())
    svc = EmailAnalysisService(llm_service=llm, cache=_MemCache(), clock=lambda: "2026-01-01T00:00:00+00:00")
    a1 = svc.analyze(_email())
    a2 = svc.analyze(_email())  # cache hit
    assert llm.calls == 1
    assert a1.category == a2.category == "Finance"
    assert a1.analyzed_at == "2026-01-01T00:00:00+00:00"


def test_service_force_reanalyzes():
    llm = _FakeLLM(_analysis())
    svc = EmailAnalysisService(llm_service=llm, cache=_MemCache())
    svc.analyze(_email())
    svc.analyze(_email(), force=True)
    assert llm.calls == 2


def test_service_get_cached_does_not_call_llm():
    llm = _FakeLLM(_analysis())
    svc = EmailAnalysisService(llm_service=llm, cache=_MemCache())
    assert svc.get_cached("m1") is None
    assert llm.calls == 0


def test_sqlite_cache_roundtrip(tmp_path: Path):
    db = SQLiteManager(tmp_path / "a.db")
    cache = SQLiteAnalysisCache(db)
    analysis = replace(_analysis(), analyzed_at="2026-01-01T00:00:00+00:00", model="gpt-test")
    cache.set("m1", analysis)
    got = cache.get("m1")
    assert got is not None
    assert (got.category, got.priority, got.needs_reply, got.action_recommended) == (
        "Finance", "High", True, "Reply"
    )
    assert got.model == "gpt-test"
    many = cache.get_many(["m1", "missing"])
    assert set(many) == {"m1"}
