from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AttachmentMetadata:
    has_attachments: bool
    filenames: list[str]


def detect_attachments(payload: dict) -> AttachmentMetadata:
    filenames: list[str] = []

    def _walk(part: dict) -> None:
        filename = str(part.get("filename", "")).strip()
        body = part.get("body", {}) or {}
        attachment_id = body.get("attachmentId")
        if filename and attachment_id:
            filenames.append(filename)
        for child in part.get("parts", []) or []:
            _walk(child)

    _walk(payload)
    return AttachmentMetadata(has_attachments=bool(filenames), filenames=filenames)

