from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class AttachmentInfo:
    """One attachment's metadata. Content is never downloaded here.

    ``attachment_id`` is the Gmail attachment handle, kept so a later phase can
    fetch the bytes on demand (preview) without changing the list/read shape.
    """

    name: str
    mime_type: str
    size: int
    attachment_id: str


@dataclass(slots=True)
class AttachmentMetadata:
    has_attachments: bool
    filenames: list[str]
    attachments: list[AttachmentInfo] = field(default_factory=list)


def detect_attachments(payload: dict) -> AttachmentMetadata:
    """Walk a Gmail message payload and collect attachment metadata only.

    A part is treated as an attachment when it has both a filename and an
    ``attachmentId`` (inline body parts have neither). Only metadata is read —
    the attachment data is never decoded or downloaded.
    """
    attachments: list[AttachmentInfo] = []

    def _walk(part: dict) -> None:
        filename = str(part.get("filename", "")).strip()
        body = part.get("body", {}) or {}
        attachment_id = body.get("attachmentId")
        if filename and attachment_id:
            attachments.append(
                AttachmentInfo(
                    name=filename,
                    mime_type=str(part.get("mimeType", "") or "application/octet-stream"),
                    size=int(body.get("size", 0) or 0),
                    attachment_id=str(attachment_id),
                )
            )
        for child in part.get("parts", []) or []:
            _walk(child)

    _walk(payload)
    return AttachmentMetadata(
        has_attachments=bool(attachments),
        filenames=[a.name for a in attachments],
        attachments=attachments,
    )
