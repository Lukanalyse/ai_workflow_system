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
