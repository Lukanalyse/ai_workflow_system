"""Archive Workspace — read-side service over Gmail labels (Phase 7).

Turns Gmail's user labels into browsable "folders". It is deliberately built
from the existing bricks and adds **no** new AI: filing still happens in Smart
Archive; this service only *reads* what was filed and lets the user move emails
back to the inbox.

Performance contract (the whole reason this is its own service):

    Archive tab            -> list_folders()      (1 labels.list + 1 labels.get
                                                    per user label — counts only)
    user opens "Finance"   -> list_emails(id)     (one page of that label only)
    user clicks "Restore"  -> restore_to_inbox()  (one batchModify)

A label's messages are never enumerated to show a count (``labels.get`` carries
``messagesTotal``/``messagesUnread``), and a folder is never fully loaded —
``list_emails`` is paged via Gmail's ``nextPageToken``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.email_service import EmailCandidate, EmailService
from app.services.mailbox_service import ActionResult, MailboxService
from app.providers.base import EmailProvider

logger = logging.getLogger(__name__)

# Default emails per page when a folder is opened. Kept modest so opening a big
# label is instant; the UI pages with the returned token for the rest.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


@dataclass(slots=True)
class ArchiveFolder:
    """A Gmail user label presented as a workspace folder."""

    id: str
    name: str
    total: int
    unread: int

    @property
    def read(self) -> int:
        return max(0, self.total - self.unread)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "total": self.total,
            "unread": self.unread,
            "read": self.read,
        }


class ArchiveService:
    """Browse filed emails by label, and restore them to the inbox.

    Composes the existing layers rather than touching Gmail directly:
    ``provider`` for label metadata/counts, ``email_service`` for the same
    candidate enrichment the inbox uses, and ``mailbox_service`` for the restore
    mutation (so scope checks and ``ActionResult`` reporting are shared).
    """

    def __init__(
        self,
        *,
        provider: EmailProvider,
        email_service: EmailService,
        mailbox_service: MailboxService,
    ) -> None:
        self._provider = provider
        self._email_service = email_service
        self._mailbox = mailbox_service

    def list_folders(self, *, include_empty: bool = False) -> list[ArchiveFolder]:
        """User labels as folders, with total/read/unread counts.

        System labels (INBOX, SENT, …) are excluded — only user labels are
        "folders". Sorted by size so the busiest folders surface first; empty
        labels are hidden unless ``include_empty`` is set.
        """
        folders: list[ArchiveFolder] = []
        for label in self._mailbox.list_labels():
            if label.get("type") != "user":
                continue
            label_id = label.get("id")
            if not label_id:
                continue
            try:
                total, unread = self._provider.label_counts(label_id)
            except Exception:  # noqa: BLE001 - one bad label must not hide the rest
                logger.exception("Failed to read counts for label %s", label_id)
                total, unread = 0, 0
            if total <= 0 and not include_empty:
                continue
            folders.append(
                ArchiveFolder(id=label_id, name=label.get("name", ""), total=total, unread=unread)
            )
        folders.sort(key=lambda f: (-f.total, f.name.lower()))
        return folders

    def list_emails(
        self, label_id: str, *, page_token: str | None = None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> dict:
        """One page of a folder's emails (same shape as the inbox feed) + next token."""
        label_id = (label_id or "").strip()
        if not label_id:
            raise ValueError("A label id is required.")
        size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        candidates, next_token = self._email_service.list_label_candidates(
            label_id, page_token=page_token, page_size=size
        )
        return {
            "label_id": label_id,
            "emails": candidates,
            "next_page_token": next_token,
        }

    def restore_to_inbox(self, message_ids: list[str]) -> ActionResult:
        """Move emails back to the inbox (keeps their filing label)."""
        return self._mailbox.restore_to_inbox(message_ids)
