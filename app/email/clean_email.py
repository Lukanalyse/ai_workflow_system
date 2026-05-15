from __future__ import annotations

import re
from html import unescape

from app.email.thread_parser import trim_thread_history


def strip_html(html_content: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", html_content)
    return re.sub(r"\s+", " ", unescape(no_tags)).strip()


def strip_signature(text: str) -> str:
    patterns = [
        r"(?im)^\s*best regards[,\s].*$",
        r"(?im)^\s*kind regards[,\s].*$",
        r"(?im)^\s*sincerely[,\s].*$",
        r"(?im)^\s*cordially[,\s].*$",
        r"(?im)^\s*sent from my.*$",
        r"(?im)^\s*--\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return text[: match.start()].strip()
    return text.strip()


def clean_email_body(raw_content: str, *, is_html: bool) -> str:
    text = strip_html(raw_content) if is_html else raw_content
    text = trim_thread_history(text)
    text = strip_signature(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def prepare_untrusted_email_for_llm(
    raw_content: str,
    *,
    is_html: bool,
    max_chars: int,
    attachment_names: list[str] | None = None,
) -> str:
    cleaned = clean_email_body(raw_content, is_html=is_html)
    clipped = cleaned[: max(1, max_chars)].strip()
    attachment_note = (
        "Attachments detected (metadata only, content unavailable): " + ", ".join(attachment_names)
        if attachment_names
        else "No attachment content available."
    )
    return (
        "UNTRUSTED_EMAIL_START\n"
        f"{clipped}\n"
        "UNTRUSTED_EMAIL_END\n"
        f"{attachment_note}"
    )
