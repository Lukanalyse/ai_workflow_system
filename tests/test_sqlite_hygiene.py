"""Phase 8.5 tests: SQLite hygiene (WAL, pragmas, sender expression index).

These are pure performance/robustness settings — they must not change any query
result, only how fast/safely the queries run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.database.sqlite_manager import GmailProcessedEmailRecord, SQLiteManager


@pytest.fixture()
def db(tmp_path: Path) -> SQLiteManager:
    return SQLiteManager(tmp_path / "test.db")


def _record(sender: str, *, mid: str = "m1") -> GmailProcessedEmailRecord:
    now = "2026-01-01T00:00:00+00:00"
    return GmailProcessedEmailRecord(
        message_id=mid, thread_id="t" + mid, subject="s", sender=sender,
        received_at=now, snippet="x", processed_status="processed",
        draft_created=True, draft_id="d1", skip_reason=None, summary=None,
        intent_label=None, urgency_score=None, confidence_score=None,
        draft_text=None, created_at=now, updated_at=now,
    )


def test_wal_mode_is_enabled(db: SQLiteManager) -> None:
    with db._connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_connection_pragmas_applied(db: SQLiteManager) -> None:
    with db._connect() as conn:
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    assert int(busy) == 5000
    assert int(sync) == 1  # NORMAL


def test_user_version_bumped(db: SQLiteManager) -> None:
    with db._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_sender_expression_index_exists(db: SQLiteManager) -> None:
    with db._connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
    assert "idx_gmail_sender_lower" in names


def test_known_senders_query_uses_the_index(db: SQLiteManager) -> None:
    db.save_gmail_processed_email(_record("Alice@X.com"))
    with db._connect() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT DISTINCT LOWER(sender) FROM gmail_processed_emails "
            "WHERE LOWER(sender) IN ('alice@x.com')"
        ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "idx_gmail_sender_lower" in detail  # index seek, not a full scan


def test_known_senders_result_unchanged(db: SQLiteManager) -> None:
    db.save_gmail_processed_email(_record("Alice@X.com", mid="m1"))
    db.save_gmail_processed_email(_record("bob@x.com", mid="m2"))
    known = db.known_senders(["alice@x.com", "BOB@X.COM", "carol@x.com"])
    assert known == {"alice@x.com", "bob@x.com"}
    assert db.sender_seen("ALICE@x.com") is True
    assert db.sender_seen("nobody@x.com") is False
