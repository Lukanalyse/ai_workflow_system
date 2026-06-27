from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.providers.base import EmailProvider

logger = logging.getLogger(__name__)

# Well-known Gmail system labels used by the high-level actions.
LABEL_UNREAD = "UNREAD"
LABEL_INBOX = "INBOX"


@dataclass(slots=True)
class ActionResult:
    """Outcome of a mailbox action over one or many messages."""

    action: str
    requested: int
    modified: int
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "requested": self.requested,
            "modified": self.modified,
            "failed": len(self.failures),
            "failures": self.failures,
        }


class MailboxService:
    """Central "Gmail Actions" layer used by every mailbox-mutating feature.

    Every action accepts one or many message ids and is provider-agnostic, so
    upcoming features (Smart Archive, AI Labels, Auto Classification, Auto
    Filing, a Rules Engine) compose these primitives instead of touching Gmail
    directly:

        ensure_label("Finance") -> apply_label(ids, label_id) -> archive(ids)

    Actions are batch-first and fail as a unit per call (Gmail's batchModify is
    atomic per request); the failure is captured in ``ActionResult`` so a bulk
    run reports cleanly rather than raising. Scope/auth problems (PermissionError)
    propagate so the web layer can prompt a reconnect.
    """

    def __init__(self, provider: EmailProvider) -> None:
        self._provider = provider

    # --- read state ----------------------------------------------------------
    def mark_read(self, message_ids: list[str]) -> ActionResult:
        return self._modify("mark_read", message_ids, remove=[LABEL_UNREAD])

    def mark_unread(self, message_ids: list[str]) -> ActionResult:
        return self._modify("mark_unread", message_ids, add=[LABEL_UNREAD])

    # --- archive -------------------------------------------------------------
    def archive(self, message_ids: list[str]) -> ActionResult:
        """Classic Gmail archive: remove from the inbox (no label applied)."""
        return self._modify("archive", message_ids, remove=[LABEL_INBOX])

    def restore_to_inbox(self, message_ids: list[str]) -> ActionResult:
        """Inverse of archive: put messages back in the inbox.

        Any filing label they carry (e.g. Finance) is kept — restoring only adds
        ``INBOX`` back, so the email reappears in the inbox without losing how it
        was filed. Used by the Archive workspace's "Restore to Inbox" action.
        """
        return self._modify("restore", message_ids, add=[LABEL_INBOX])

    # --- labels --------------------------------------------------------------
    def apply_label(
        self,
        message_ids: list[str],
        *,
        label_id: str,
        archive: bool = False,
        remove_labels: list[str] | None = None,
    ) -> ActionResult:
        """Apply an existing label to messages, optionally archiving too.

        ``archive=True`` is the seam Smart Archive / Auto Filing uses to
        file-and-archive in one step. ``remove_labels`` strips other labels in
        the same call — used when *re*-filing an already-archived email so it
        leaves its previous folder instead of ending up in two at once.
        """
        remove = [r for r in (remove_labels or []) if r and r != label_id]
        if archive:
            remove.append(LABEL_INBOX)
        return self._modify(
            "apply_label", message_ids, add=[label_id], remove=remove or None
        )

    def list_labels(self) -> list[dict]:
        labels = self._provider.list_labels()
        return [{"id": l.id, "name": l.name, "type": l.type} for l in labels]

    def create_label(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("Label name is empty.")
        label = self._provider.create_label(name)
        return {"id": label.id, "name": label.name, "type": label.type}

    def ensure_label(self, name: str) -> dict:
        """Find a user label by name (case-insensitive) or create it.

        The find-or-create primitive that future automatic-classification /
        smart-archive flows build on.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("Label name is empty.")
        for label in self._provider.list_labels():
            if label.name.lower() == name.lower() and label.type == "user":
                return {"id": label.id, "name": label.name, "type": label.type}
        return self.create_label(name)

    # --- internals -----------------------------------------------------------
    def _modify(
        self,
        action: str,
        message_ids: list[str],
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> ActionResult:
        ids = [m for m in dict.fromkeys(message_ids or []) if m]
        if not ids:
            return ActionResult(action=action, requested=0, modified=0)
        try:
            modified = self._provider.modify_labels(ids, add=add, remove=remove)
            logger.info("%s: modified %d/%d message(s)", action, modified, len(ids))
            return ActionResult(action=action, requested=len(ids), modified=modified)
        except PermissionError:
            # Missing scope / auth — let the web layer turn this into a 403 +
            # reconnect prompt rather than swallowing it as a per-item failure.
            raise
        except Exception as exc:  # noqa: BLE001 - report, don't crash the request
            logger.exception("%s failed for %d message(s)", action, len(ids))
            return ActionResult(
                action=action,
                requested=len(ids),
                modified=0,
                failures=[{"message_ids": ids, "error": str(exc)}],
            )
