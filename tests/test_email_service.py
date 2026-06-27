"""Phase 0 backend tests: attachment enrichment + batched DB lookups.

The bulk helpers (``seen_status_bulk`` / ``known_senders``) must return exactly
what the per-email path (``already_processed_gmail`` / ``sender_seen``) would,
so the inbox listing keeps identical replyability/seen behavior while doing far
fewer queries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.database.sqlite_manager import GmailProcessedEmailRecord, SQLiteManager
from app.email.attachment_detector import detect_attachments
from app.email.gmail_reader import GmailReader, GmailReadConfig


@pytest.fixture()
def db(tmp_path: Path) -> SQLiteManager:
    return SQLiteManager(tmp_path / "test.db")


def _record(message_id: str, thread_id: str, sender: str, *, draft: bool, status: str) -> GmailProcessedEmailRecord:
    now = "2026-01-01T00:00:00+00:00"
    return GmailProcessedEmailRecord(
        message_id=message_id,
        thread_id=thread_id,
        subject="s",
        sender=sender,
        received_at=now,
        snippet="x",
        processed_status=status,
        draft_created=draft,
        draft_id="d1" if draft else None,
        skip_reason=None,
        summary=None,
        intent_label=None,
        urgency_score=None,
        confidence_score=None,
        draft_text=None,
        created_at=now,
        updated_at=now,
    )


def test_seen_status_bulk_matches_per_email(db: SQLiteManager) -> None:
    db.save_gmail_processed_email(_record("m1", "t1", "a@x.com", draft=True, status="processed"))
    db.save_gmail_processed_email(_record("m2", "t2", "b@x.com", draft=False, status="skipped"))
    db.save_gmail_processed_email(_record("m3", "t3", "c@x.com", draft=False, status="pending"))

    # A 4th message sharing t1's thread should be "seen" via the thread draft.
    candidates = [("m1", "t1"), ("m2", "t2"), ("m3", "t3"), ("m4", "t1"), ("m5", "t9")]
    msg_ids = [m for m, _ in candidates]
    thread_ids = [t for _, t in candidates]

    seen_messages, draft_threads = db.seen_status_bulk(msg_ids, thread_ids)

    for mid, tid in candidates:
        per_email, _ = db.already_processed_gmail(mid, tid)
        bulk = mid in seen_messages or tid in draft_threads
        assert bulk == per_email, f"{mid}/{tid}: bulk={bulk} per_email={per_email}"

    # Spot-check the expected outcomes explicitly.
    assert {"m1", "m4"} <= {m for m, t in candidates if (m in seen_messages or t in draft_threads)}
    assert "m3" not in seen_messages and "t3" not in draft_threads
    assert "m5" not in seen_messages and "t9" not in draft_threads


def test_known_senders_matches_per_email(db: SQLiteManager) -> None:
    db.save_gmail_processed_email(_record("m1", "t1", "Alice@X.com", draft=True, status="processed"))
    db.save_gmail_processed_email(_record("m2", "t2", "bob@x.com", draft=False, status="skipped"))

    senders = ["alice@x.com", "BOB@X.COM", "carol@x.com", ""]
    known = db.known_senders(senders)

    for s in senders:
        assert (s.strip().lower() in known) == db.sender_seen(s)
    assert known == {"alice@x.com", "bob@x.com"}


def test_bulk_helpers_handle_empty_input(db: SQLiteManager) -> None:
    assert db.seen_status_bulk([], []) == (set(), set())
    assert db.known_senders([]) == set()


def test_detect_attachments_captures_metadata_only() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "filename": "",
        "body": {},
        "parts": [
            {"mimeType": "text/plain", "filename": "", "body": {"data": "aGk="}},
            {"mimeType": "application/pdf", "filename": "rapport.pdf", "body": {"attachmentId": "abc", "size": 1234}},
            {
                "mimeType": "multipart/related",
                "filename": "",
                "body": {},
                "parts": [
                    {"mimeType": "image/png", "filename": "photo.png", "body": {"attachmentId": "def", "size": 555}},
                ],
            },
        ],
    }
    meta = detect_attachments(payload)
    assert meta.has_attachments is True
    assert meta.filenames == ["rapport.pdf", "photo.png"]
    first = meta.attachments[0]
    assert (first.name, first.mime_type, first.size, first.attachment_id) == (
        "rapport.pdf",
        "application/pdf",
        1234,
        "abc",
    )
    # No body data is ever surfaced — only metadata fields exist on AttachmentInfo.
    assert not hasattr(first, "data")


def test_detect_attachments_none() -> None:
    payload = {"mimeType": "text/plain", "filename": "", "body": {"data": "aGk="}}
    meta = detect_attachments(payload)
    assert meta.has_attachments is False
    assert meta.attachments == []


# --- Phase 2: status query builder + pagination ------------------------------
def test_build_query_status_variants() -> None:
    r = GmailReader.__new__(GmailReader)  # _build_query needs no live service
    assert "is:unread" in r._build_query(GmailReadConfig())  # default unchanged
    assert "is:read" in r._build_query(GmailReadConfig(status="read"))
    all_q = r._build_query(GmailReadConfig(status="all"))
    assert "is:unread" not in all_q and "is:read" not in all_q
    # Legacy callers (only_unread=False, no status) get the unconstrained query.
    assert "is:unread" not in r._build_query(GmailReadConfig(only_unread=False))


class _FakeMessages:
    """Minimal Gmail messages() resource that paginates a fixed id list."""

    def __init__(self, ids: list[str], page_size: int) -> None:
        self._ids = ids
        self._page = page_size

    def list(self, *, userId, maxResults, q, pageToken=None):
        start = int(pageToken or 0)
        end = min(start + min(maxResults, self._page), len(self._ids))
        next_token = str(end) if end < len(self._ids) else None
        payload = {"messages": [{"id": i} for i in self._ids[start:end]]}
        if next_token:
            payload["nextPageToken"] = next_token
        return _Exec(payload)


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


def test_collect_ids_paginates_to_target() -> None:
    r = GmailReader.__new__(GmailReader)
    r.user_id = "me"
    ids = [f"m{i}" for i in range(120)]

    class _Svc:
        def __init__(self, msgs):
            self._msgs = msgs

        def users(self):
            return self

        def messages(self):
            return self._msgs

    # Page size 50 forces multiple pages; target 120 must gather all 120.
    r.service = _Svc(_FakeMessages(ids, page_size=50))
    assert r._collect_ids("q", 120) == ids
    # A smaller target stops early without over-fetching.
    assert r._collect_ids("q", 30) == ids[:30]


# --- Phase 7: label listing + counts (Archive workspace) ---------------------
class _LabelMessages:
    """messages() resource that lists by labelIds (one page) and gets bodies."""

    def __init__(self, by_label, bodies) -> None:
        self._by_label = by_label   # {label_id: (ids, next_token)} for page_token=None
        self._bodies = bodies       # {id: minimal Gmail message payload}
        self.list_args: list[dict] = []

    def list(self, *, userId, labelIds, maxResults, pageToken=None, includeSpamTrash=None):
        self.list_args.append(
            {"labelIds": labelIds, "maxResults": maxResults, "pageToken": pageToken,
             "includeSpamTrash": includeSpamTrash}
        )
        ids, next_token = self._by_label.get(labelIds[0], ([], None))
        payload = {"messages": [{"id": i} for i in ids]}
        if next_token:
            payload["nextPageToken"] = next_token
        return _Exec(payload)

    def get(self, *, userId, id, format):
        return _Exec(self._bodies[id])


class _Labels:
    def __init__(self, counts) -> None:
        self._counts = counts  # {label_id: {"messagesTotal": n, "messagesUnread": m}}
        self.get_calls: list[str] = []

    def get(self, *, userId, id):
        self.get_calls.append(id)
        return _Exec(self._counts[id])


class _LabelSvc:
    def __init__(self, messages=None, labels=None) -> None:
        self._messages = messages
        self._labels = labels

    def users(self):
        return self

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels


def _body(mid: str) -> dict:
    return {
        "id": mid,
        "threadId": "t_" + mid,
        "internalDate": "1700000000000",
        "snippet": "snippet " + mid,
        "labelIds": ["L_fin"],
        "payload": {"headers": [{"name": "From", "value": "Acme <a@acme.com>"},
                                {"name": "Subject", "value": "Re: " + mid}]},
    }


def test_get_label_counts() -> None:
    r = GmailReader.__new__(GmailReader)
    r.user_id = "me"
    labels = _Labels({"L_fin": {"messagesTotal": 24, "messagesUnread": 5}})
    r.service = _LabelSvc(labels=labels)
    assert r.get_label_counts("L_fin") == (24, 5)
    assert labels.get_calls == ["L_fin"]


def test_list_by_label_returns_messages_and_token() -> None:
    r = GmailReader.__new__(GmailReader)
    r.user_id = "me"
    msgs = _LabelMessages(
        by_label={"L_fin": (["m1", "m2"], "TOK2")},
        bodies={"m1": _body("m1"), "m2": _body("m2")},
    )
    r.service = _LabelSvc(messages=msgs)
    messages, next_token = r.list_by_label("L_fin", page_size=2)
    assert [m.id for m in messages] == ["m1", "m2"]
    assert next_token == "TOK2"
    # Filtered by labelIds (not a free-text query) and excludes spam/trash.
    assert msgs.list_args[0]["labelIds"] == ["L_fin"]
    assert msgs.list_args[0]["includeSpamTrash"] is False
    assert messages[0].subject == "Re: m1" and messages[0].sender_email == "a@acme.com"
