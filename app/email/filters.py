from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable


@dataclass(slots=True)
class EmailFilterConfig:
    only_unread: bool = True
    after_date: datetime | None = None
    sender_whitelist: set[str] | None = None
    sender_blacklist: set[str] | None = None
    keywords: set[str] | None = None
    exclude_newsletters: bool = True
    exclude_automated: bool = True

    @classmethod
    def from_parts(
        cls,
        only_unread: bool,
        after_date: datetime | None,
        sender_whitelist: Iterable[str],
        sender_blacklist: Iterable[str],
        keywords: Iterable[str],
        exclude_newsletters: bool,
        exclude_automated: bool,
    ) -> "EmailFilterConfig":
        return cls(
            only_unread=only_unread,
            after_date=after_date,
            sender_whitelist={item.lower() for item in sender_whitelist},
            sender_blacklist={item.lower() for item in sender_blacklist},
            keywords={item.lower() for item in keywords},
            exclude_newsletters=exclude_newsletters,
            exclude_automated=exclude_automated,
        )


def build_graph_filter(config: EmailFilterConfig) -> str:
    clauses: list[str] = []
    if config.only_unread:
        clauses.append("isRead eq false")
    if config.after_date:
        after = config.after_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        clauses.append(f"receivedDateTime ge {after}")
    return " and ".join(clauses)


def is_newsletter(subject: str, sender: str, body: str) -> bool:
    text = f"{subject} {sender} {body}".lower()
    markers = {"unsubscribe", "newsletter", "view in browser", "mailing list", "digest"}
    return any(marker in text for marker in markers)


def is_automated(sender: str, subject: str) -> bool:
    sender_l = sender.lower()
    subject_l = subject.lower()
    markers = {"noreply", "no-reply", "do-not-reply", "automated", "notification"}
    return any(marker in sender_l or marker in subject_l for marker in markers)


def match_filters(
    config: EmailFilterConfig,
    *,
    sender: str,
    subject: str,
    body: str,
    received_at: datetime,
) -> bool:
    sender_l = sender.lower()
    subject_l = subject.lower()
    body_l = body.lower()

    if config.after_date and received_at < config.after_date:
        return False
    if config.sender_whitelist and sender_l not in config.sender_whitelist:
        return False
    if config.sender_blacklist and sender_l in config.sender_blacklist:
        return False
    if config.exclude_newsletters and is_newsletter(subject_l, sender_l, body_l):
        return False
    if config.exclude_automated and is_automated(sender_l, subject_l):
        return False
    if config.keywords:
        text = f"{subject_l}\n{body_l}"
        if not any(keyword in text for keyword in config.keywords):
            return False
    return True

