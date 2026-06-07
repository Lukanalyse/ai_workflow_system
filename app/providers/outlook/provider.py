from __future__ import annotations

from app.config.settings import AppSettings
from app.providers.base import (
    DraftResult,
    EmailListConfig,
    EmailMessage,
    EmailProvider,
    SentResult,
)

# Placeholder for a future Microsoft Graph / Outlook backend. The class exists
# so the provider architecture (and any registry/UI selector) can reference it,
# but no methods are implemented yet.


class OutlookProvider(EmailProvider):
    """Not implemented. Reserved for an Outlook / Microsoft Graph backend."""

    name = "outlook"

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    def list_messages(self, config: EmailListConfig) -> list[EmailMessage]:
        raise NotImplementedError("Outlook provider is not implemented yet.")

    def get_message(self, message_id: str) -> EmailMessage:
        raise NotImplementedError("Outlook provider is not implemented yet.")

    def create_draft(self, message: EmailMessage, body: str) -> DraftResult:
        raise NotImplementedError("Outlook provider is not implemented yet.")

    def send_reply(self, message: EmailMessage, body: str) -> SentResult:
        raise NotImplementedError("Outlook provider is not implemented yet.")

    def evaluate_replyability(self, message: EmailMessage) -> tuple[bool, str]:
        raise NotImplementedError("Outlook provider is not implemented yet.")

    def health(self) -> tuple[str, str]:
        return "disabled", "Outlook provider not implemented."
