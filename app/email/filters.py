from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True)
class EmailFilterConfig:
    only_unread: bool = True
    after_date: datetime | None = None
    sender_filter: str | None = None
    exclude_promotions: bool = True
    exclude_noreply: bool = True

    @classmethod
    def from_parts(
        cls,
        only_unread: bool,
        after_date: datetime | None,
        sender_filter: str | None,
        exclude_promotions: bool,
        exclude_noreply: bool,
    ) -> "EmailFilterConfig":
        return cls(
            only_unread=only_unread,
            after_date=after_date,
            sender_filter=(sender_filter or "").strip().lower() or None,
            exclude_promotions=exclude_promotions,
            exclude_noreply=exclude_noreply,
        )


def build_gmail_query(config: EmailFilterConfig) -> str:
    clauses: list[str] = ["in:inbox", "-in:spam", "-in:trash"]
    if config.only_unread:
        clauses.append("is:unread")
    if config.after_date:
        after = config.after_date.astimezone(timezone.utc).strftime("%Y/%m/%d")
        clauses.append(f"after:{after}")
    if config.exclude_promotions:
        clauses.extend(["-category:promotions", "-category:social"])
    if config.sender_filter:
        clauses.append(f"from:{config.sender_filter}")
    return " ".join(clauses)


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
    if config.sender_filter and config.sender_filter not in sender_l:
        return False
    if config.exclude_promotions and is_newsletter(subject_l, sender_l, body_l):
        return False
    if config.exclude_noreply and is_automated(sender_l, subject_l):
        return False
    return True
