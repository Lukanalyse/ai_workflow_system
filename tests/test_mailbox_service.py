"""Phase 3 tests: the central Gmail Actions layer (MailboxService).

Covers the cases called out in the spec:
- single email, multiple emails
- Gmail/network error (isolated into the result, not raised)
- missing scope / lost connection (PermissionError propagates for a 403 + reconnect)
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.mailbox_service import MailboxService


@dataclass
class _Label:
    id: str
    name: str
    type: str = "user"


class FakeProvider:
    """Minimal provider stand-in capturing modify calls (duck-typed)."""

    def __init__(self, *, raise_exc: Exception | None = None, labels: list[_Label] | None = None) -> None:
        self.calls: list[dict] = []
        self._raise = raise_exc
        self._labels = labels or []
        self.created: list[str] = []

    def modify_labels(self, message_ids, *, add=None, remove=None) -> int:
        if self._raise is not None:
            raise self._raise
        self.calls.append({"ids": list(message_ids), "add": add or [], "remove": remove or []})
        return len(message_ids)

    def list_labels(self):
        return list(self._labels)

    def create_label(self, name: str):
        self.created.append(name)
        label = _Label(id=f"Label_{len(self._labels) + 1}", name=name)
        self._labels.append(label)
        return label


def test_mark_read_single() -> None:
    p = FakeProvider()
    r = MailboxService(p).mark_read(["m1"])
    assert r.modified == 1 and r.requested == 1 and not r.failures
    assert p.calls == [{"ids": ["m1"], "add": [], "remove": ["UNREAD"]}]


def test_mark_unread_multiple_dedups() -> None:
    p = FakeProvider()
    r = MailboxService(p).mark_unread(["m1", "m2", "m2", "m3"])
    assert r.modified == 3 and r.requested == 3  # duplicate collapsed
    assert p.calls[0]["add"] == ["UNREAD"] and p.calls[0]["remove"] == []


def test_archive_removes_inbox_for_many() -> None:
    p = FakeProvider()
    r = MailboxService(p).archive([f"m{i}" for i in range(10)])
    assert r.modified == 10
    assert p.calls[0]["remove"] == ["INBOX"] and p.calls[0]["add"] == []


def test_apply_label_with_archive() -> None:
    p = FakeProvider()
    r = MailboxService(p).apply_label(["m1", "m2"], label_id="Label_7", archive=True)
    assert r.modified == 2
    assert p.calls[0]["add"] == ["Label_7"] and p.calls[0]["remove"] == ["INBOX"]


def test_empty_ids_is_a_noop() -> None:
    p = FakeProvider()
    r = MailboxService(p).archive([])
    assert r.requested == 0 and r.modified == 0 and not p.calls


def test_ensure_label_finds_existing_case_insensitive() -> None:
    p = FakeProvider(labels=[_Label(id="Label_9", name="Finance")])
    out = MailboxService(p).ensure_label("finance")
    assert out["id"] == "Label_9" and not p.created  # no duplicate created


def test_ensure_label_creates_when_missing() -> None:
    p = FakeProvider(labels=[])
    out = MailboxService(p).ensure_label("Research")
    assert out["name"] == "Research" and p.created == ["Research"]


def test_missing_scope_propagates_permission_error() -> None:
    # Lost scope / not connected -> PermissionError must NOT be swallowed, so the
    # web layer can answer 403 and prompt a reconnect.
    p = FakeProvider(raise_exc=PermissionError("reconnect Gmail"))
    with pytest.raises(PermissionError):
        MailboxService(p).mark_read(["m1"])


def test_gmail_error_is_isolated_into_result() -> None:
    # A generic Gmail/network error becomes a clean failure entry, not a raise.
    p = FakeProvider(raise_exc=RuntimeError("Gmail 500"))
    r = MailboxService(p).archive(["m1", "m2"])
    assert r.modified == 0 and len(r.failures) == 1
    assert "Gmail 500" in r.failures[0]["error"]


def test_as_dict_shape() -> None:
    p = FakeProvider()
    d = MailboxService(p).mark_read(["m1", "m2"]).as_dict()
    assert d == {"action": "mark_read", "requested": 2, "modified": 2, "failed": 0, "failures": []}
