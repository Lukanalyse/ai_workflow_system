from __future__ import annotations

import logging
from dataclasses import dataclass

from googleapiclient.discovery import Resource

from app.email import gmail_http

logger = logging.getLogger(__name__)

# Gmail's batchModify accepts up to 1000 message ids per call.
_BATCH_MODIFY_MAX = 1000


def _chunks(items: list[str], size: int = _BATCH_MODIFY_MAX):
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass(slots=True)
class GmailLabel:
    id: str
    name: str
    type: str  # "user" | "system"


class GmailActions:
    """Low-level Gmail mailbox mutations (label/archive/read-state).

    This is the single place that touches Gmail's modify surface, mirroring the
    GmailReader/GmailDraftCreator split. It requires the ``gmail.modify`` scope;
    callers (the provider) check for the grant first and surface a clean
    reconnect prompt, so a missing scope never reaches here as a raw 403.

    Everything is batch-first (``batchModify``) so one email and a thousand
    emails cost the same number of round-trips per 1000.
    """

    _http_factory = None

    def __init__(self, service: Resource, user_id: str = "me", *, http_factory=None) -> None:
        self.service = service
        self.user_id = user_id
        self._http_factory = http_factory

    # --- message label mutations --------------------------------------------
    def batch_modify(
        self,
        message_ids: list[str],
        *,
        add_label_ids: list[str] | None = None,
        remove_label_ids: list[str] | None = None,
    ) -> int:
        """Add/remove labels on many messages. Returns the count modified."""
        ids = [m for m in dict.fromkeys(message_ids) if m]
        if not ids:
            return 0
        add = add_label_ids or []
        remove = remove_label_ids or []
        modified = 0
        for chunk in _chunks(ids):
            body = {"ids": chunk}
            if add:
                body["addLabelIds"] = add
            if remove:
                body["removeLabelIds"] = remove
            gmail_http.execute(
                self.service.users().messages().batchModify(userId=self.user_id, body=body),
                op="messages.batchModify",
            )
            modified += len(chunk)
        logger.info(
            "batchModify on %d message(s): add=%s remove=%s", modified, add, remove
        )
        return modified

    # --- labels --------------------------------------------------------------
    def list_labels(self) -> list[GmailLabel]:
        resp = gmail_http.execute(
            self.service.users().labels().list(userId=self.user_id), op="labels.list"
        )
        labels = [
            GmailLabel(id=str(l.get("id")), name=str(l.get("name")), type=str(l.get("type", "user")))
            for l in resp.get("labels", [])
            if l.get("id") and l.get("name")
        ]
        return labels

    def create_label(self, name: str) -> GmailLabel:
        body = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = gmail_http.execute(
            self.service.users().labels().create(userId=self.user_id, body=body),
            op="labels.create",
        )
        logger.info("Created Gmail label %r (id=%s)", name, created.get("id"))
        return GmailLabel(
            id=str(created.get("id")), name=str(created.get("name", name)), type="user"
        )
