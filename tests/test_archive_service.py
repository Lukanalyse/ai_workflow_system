"""Phase 7 tests: the Archive Workspace read service (ArchiveService).

Covers what the spec calls out:
- folders = user labels with total/read/unread counts (system labels excluded,
  empty labels hidden, busiest first)
- opening a folder loads ONLY one page and passes the page token through
- a flaky label's counts never hide the other folders
- restore moves emails back to the inbox (keeping their label) and missing
  scope still propagates as a PermissionError for a 403 + reconnect.
"""

from __future__ import annotations

import pytest

from app.services.archive_service import ArchiveService
from app.services.mailbox_service import ActionResult, MailboxService


class FakeProvider:
    """Provider stand-in for label metadata, counts and paged listing."""

    def __init__(self, *, labels, counts, pages=None, restore_exc=None) -> None:
        self._labels = labels            # list[dict] like MailboxService.list_labels()
        self._counts = counts            # {label_id: (total, unread)}
        self._pages = pages or {}        # {(label_id, page_token): (messages, next_token)}
        self._restore_exc = restore_exc
        self.modify_calls: list[dict] = []
        self.count_calls: list[str] = []
        self.list_calls: list[dict] = []

    # used by MailboxService.list_labels()
    def list_labels(self):
        from app.email.gmail_actions import GmailLabel

        return [GmailLabel(id=l["id"], name=l["name"], type=l.get("type", "user")) for l in self._labels]

    def label_counts(self, label_id):
        self.count_calls.append(label_id)
        if isinstance(self._counts.get(label_id), Exception):
            raise self._counts[label_id]
        return self._counts.get(label_id, (0, 0))

    def list_label_messages(self, label_id, *, page_size=25, page_token=None):
        self.list_calls.append({"label_id": label_id, "page_size": page_size, "page_token": page_token})
        return self._pages.get((label_id, page_token), ([], None))

    # used by MailboxService.restore_to_inbox()
    def modify_labels(self, message_ids, *, add=None, remove=None):
        if self._restore_exc is not None:
            raise self._restore_exc
        self.modify_calls.append({"ids": list(message_ids), "add": add or [], "remove": remove or []})
        return len(message_ids)


class FakeEmailService:
    """Stand-in for EmailService.list_label_candidates (candidate enrichment)."""

    def __init__(self, provider) -> None:
        self._provider = provider

    def list_label_candidates(self, label_id, *, page_token=None, page_size=25):
        messages, next_token = self._provider.list_label_messages(
            label_id, page_size=page_size, page_token=page_token
        )
        # Return the raw messages as "candidates" — shape is irrelevant to the
        # service, which only forwards them.
        return list(messages), next_token


def _build(provider) -> ArchiveService:
    return ArchiveService(
        provider=provider,
        email_service=FakeEmailService(provider),
        mailbox_service=MailboxService(provider),
    )


def test_list_folders_counts_and_excludes_system_and_empty() -> None:
    provider = FakeProvider(
        labels=[
            {"id": "L_fin", "name": "Finance", "type": "user"},
            {"id": "L_res", "name": "Research", "type": "user"},
            {"id": "INBOX", "name": "INBOX", "type": "system"},  # excluded
            {"id": "L_empty", "name": "Empty", "type": "user"},  # hidden (0 total)
        ],
        counts={"L_fin": (24, 5), "L_res": (15, 0), "L_empty": (0, 0)},
    )
    folders = _build(provider).list_folders()

    assert [f.name for f in folders] == ["Finance", "Research"]  # busiest first, no system/empty
    fin = folders[0]
    assert (fin.total, fin.unread, fin.read) == (24, 5, 19)
    assert fin.as_dict() == {"id": "L_fin", "name": "Finance", "total": 24, "unread": 5, "read": 19}


def test_list_folders_include_empty_keeps_zero_count_labels() -> None:
    provider = FakeProvider(
        labels=[{"id": "L_empty", "name": "Empty", "type": "user"}],
        counts={"L_empty": (0, 0)},
    )
    folders = _build(provider).list_folders(include_empty=True)
    assert [f.name for f in folders] == ["Empty"]


def test_one_flaky_label_does_not_hide_the_others() -> None:
    provider = FakeProvider(
        labels=[
            {"id": "L_ok", "name": "Finance", "type": "user"},
            {"id": "L_bad", "name": "Broken", "type": "user"},
        ],
        counts={"L_ok": (10, 1), "L_bad": RuntimeError("Gmail 500")},
    )
    folders = _build(provider).list_folders(include_empty=True)
    names = {f.name: f for f in folders}
    assert names["Finance"].total == 10
    assert names["Broken"].total == 0 and names["Broken"].unread == 0  # degraded to zero, not dropped


def test_list_emails_pages_one_label_and_passes_token() -> None:
    provider = FakeProvider(
        labels=[{"id": "L_fin", "name": "Finance", "type": "user"}],
        counts={"L_fin": (40, 0)},
        pages={
            ("L_fin", None): (["m1", "m2"], "TOK2"),
            ("L_fin", "TOK2"): (["m3"], None),
        },
    )
    svc = _build(provider)

    page1 = svc.list_emails("L_fin", page_size=2)
    assert page1["label_id"] == "L_fin"
    assert page1["emails"] == ["m1", "m2"]
    assert page1["next_page_token"] == "TOK2"
    # Only the requested label was listed — never a full mailbox scan.
    assert provider.list_calls[0] == {"label_id": "L_fin", "page_size": 2, "page_token": None}

    page2 = svc.list_emails("L_fin", page_token="TOK2", page_size=2)
    assert page2["emails"] == ["m3"] and page2["next_page_token"] is None


def test_list_emails_requires_label_id() -> None:
    provider = FakeProvider(labels=[], counts={})
    with pytest.raises(ValueError):
        _build(provider).list_emails("  ")


def test_list_emails_clamps_page_size() -> None:
    provider = FakeProvider(
        labels=[{"id": "L", "name": "L", "type": "user"}],
        counts={"L": (1, 0)},
        pages={("L", None): ([], None)},
    )
    _build(provider).list_emails("L", page_size=10_000)
    assert provider.list_calls[0]["page_size"] == 100  # MAX_PAGE_SIZE


def test_restore_to_inbox_adds_inbox_keeps_label() -> None:
    provider = FakeProvider(labels=[], counts={})
    r = _build(provider).restore_to_inbox(["m1", "m2"])
    assert isinstance(r, ActionResult)
    assert r.modified == 2 and r.action == "restore"
    # Restore only re-adds INBOX; it never removes the filing label.
    assert provider.modify_calls == [{"ids": ["m1", "m2"], "add": ["INBOX"], "remove": []}]


def test_restore_missing_scope_propagates() -> None:
    provider = FakeProvider(labels=[], counts={}, restore_exc=PermissionError("reconnect Gmail"))
    with pytest.raises(PermissionError):
        _build(provider).restore_to_inbox(["m1"])
