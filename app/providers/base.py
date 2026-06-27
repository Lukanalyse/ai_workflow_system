from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from app.email.attachment_detector import AttachmentInfo


@dataclass(slots=True)
class EmailMessage:
    """Provider-neutral email representation.

    Field names intentionally match the Gmail reader's dataclass so existing
    Gmail helpers (replyability, draft creation) operate on this type directly.
    An Outlook provider would map its own payload into the same shape.
    """

    id: str
    thread_id: str
    subject: str
    sender_email: str
    sender_name: str
    internet_message_id: str | None
    received_at: datetime
    snippet: str
    body_text: str
    label_ids: list[str] = field(default_factory=list)
    has_attachments: bool = False
    attachment_names: list[str] = field(default_factory=list)
    attachments: list[AttachmentInfo] = field(default_factory=list)


@dataclass(slots=True)
class EmailListConfig:
    only_unread: bool = True
    max_emails: int = 20
    after_date: datetime | None = None
    sender_filter: str | None = None
    exclude_promotions: bool = True
    exclude_noreply: bool = True
    # Read-status filter: "unread" (default, current behavior) | "read" | "all".
    status: str = "unread"


@dataclass(slots=True)
class DraftResult:
    draft_id: str
    message_id: str
    thread_id: str


@dataclass(slots=True)
class SentResult:
    message_id: str
    thread_id: str


class EmailProvider(ABC):
    """Interface every mailbox backend (Gmail, Outlook, …) implements."""

    name: str = "email"

    @abstractmethod
    def list_messages(self, config: EmailListConfig) -> list[EmailMessage]:
        """Return candidate messages for the given filters."""

    @abstractmethod
    def get_message(self, message_id: str) -> EmailMessage:
        """Fetch a single message (with full body) by id."""

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Fetch a single attachment's raw bytes."""
        raise NotImplementedError("This provider does not support attachment download.")

    @abstractmethod
    def create_draft(self, message: EmailMessage, body: str) -> DraftResult:
        """Create a reply draft for `message`. Never sends."""

    @abstractmethod
    def send_reply(self, message: EmailMessage, body: str) -> SentResult:
        """Send a reply to `message`. Only ever called on explicit user action."""

    @abstractmethod
    def evaluate_replyability(self, message: EmailMessage) -> tuple[bool, str]:
        """Return (replyable, reason) using provider heuristics."""

    @abstractmethod
    def health(self) -> tuple[str, str]:
        """Return (status, detail) for the health endpoint."""

    # --- Mailbox mutations (Phase 3) ----------------------------------------
    # Non-abstract on purpose: providers that do not yet implement mailbox
    # actions (e.g. the Outlook scaffold) stay importable and instantiable.
    # GmailProvider overrides all of these.
    def has_modify_scope(self) -> bool:
        """Whether the active credentials may modify the mailbox."""
        return False

    def modify_labels(
        self,
        message_ids: list[str],
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> int:
        """Add/remove labels on one or many messages; return the count modified."""
        raise NotImplementedError("This provider does not support label modification.")

    def list_labels(self) -> list:
        """Return the mailbox's user labels."""
        raise NotImplementedError("This provider does not support listing labels.")

    def label_counts(self, label_id: str) -> tuple[int, int]:
        """Return (total, unread) message counts for a label."""
        raise NotImplementedError("This provider does not support label counts.")

    def label_counts_many(self, label_ids: list[str]) -> dict[str, tuple[int, int]]:
        """Return ``{label_id: (total, unread)}`` for many labels.

        Default is a per-label loop; providers that can batch (Gmail) override
        this. A per-label failure degrades to ``(0, 0)`` rather than raising.
        """
        out: dict[str, tuple[int, int]] = {}
        for label_id in label_ids:
            try:
                out[label_id] = self.label_counts(label_id)
            except Exception:  # noqa: BLE001 - one bad label must not hide the rest
                out[label_id] = (0, 0)
        return out

    def list_label_messages(
        self, label_id: str, *, page_size: int = 25, page_token: str | None = None
    ) -> tuple[list[EmailMessage], str | None]:
        """Return one page of messages carrying ``label_id`` plus the next page token."""
        raise NotImplementedError("This provider does not support listing label messages.")

    def create_label(self, name: str):
        """Create a label and return it."""
        raise NotImplementedError("This provider does not support creating labels.")
