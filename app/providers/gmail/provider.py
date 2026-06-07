from __future__ import annotations

import logging

from app.auth.gmail_auth import GmailAuthManager, build_auth_manager
from app.auth.oauth_flow import GmailOAuthFlow
from app.auth.token_store import TokenStore
from app.config.settings import AppSettings
from app.email.gmail_draft_creator import GmailDraftCreator
from app.email.gmail_reader import GmailMessage, GmailReadConfig, GmailReader
from app.email.gmail_sender import GmailSender
from app.providers.base import (
    DraftResult,
    EmailListConfig,
    EmailMessage,
    EmailProvider,
    SentResult,
)

logger = logging.getLogger(__name__)


def _to_email_message(msg: GmailMessage) -> EmailMessage:
    return EmailMessage(
        id=msg.id,
        thread_id=msg.thread_id,
        subject=msg.subject,
        sender_email=msg.sender_email,
        sender_name=msg.sender_name,
        internet_message_id=msg.internet_message_id,
        received_at=msg.received_at,
        snippet=msg.snippet,
        body_text=msg.body_text,
        label_ids=list(msg.label_ids),
        has_attachments=msg.has_attachments,
        attachment_names=list(msg.attachment_names),
    )


class GmailProvider(EmailProvider):
    """Gmail-backed EmailProvider. Reuses the existing reader/draft/auth code.

    The Gmail API service is built lazily on first use so importing the
    provider (e.g. for a health check) never triggers the OAuth flow.
    """

    name = "gmail"

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._reader: GmailReader | None = None
        self._draft_creator: GmailDraftCreator | None = None
        self._sender: GmailSender | None = None
        gmail = settings.gmail
        self._token_store = TokenStore(gmail.tokens_dir, legacy_token_path=gmail.token_path)
        self.oauth = GmailOAuthFlow(
            credentials_path=gmail.credentials_path,
            scopes=gmail.scopes,
            redirect_uri=settings.oauth_redirect_uri,
            # Persist the pending PKCE verifier under storage/ (a Docker volume)
            # so the callback survives a worker change or a container restart
            # between /connect and /callback.
            state_path=gmail.tokens_dir.parent / "oauth_state.json",
        )

    @property
    def token_store(self) -> TokenStore:
        return self._token_store

    def _active_token_path(self):
        """Resolve the token file to read: active account, else legacy file."""
        active = self._token_store.active_token_path()
        return active or self._settings.gmail.token_path

    def _auth_manager(self) -> GmailAuthManager:
        return build_auth_manager(self._settings)

    def _ensure_service(self) -> None:
        if self._reader is not None:
            return
        try:
            from googleapiclient.discovery import build
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Missing dependency: google-api-python-client. Run `pip install -r requirements.txt`."
            ) from exc
        gmail = self._settings.gmail
        creds = self._auth_manager().load_credentials()
        if creds is None:
            raise RuntimeError(
                "Gmail is not connected. Open Settings → Gmail and click "
                "“Connect Gmail” to authorize access."
            )
        service = build("gmail", "v1", credentials=creds)
        # Adopt a legacy token.json into the new store the first time we use it
        # (its account email isn't known until we can call the API).
        self._maybe_migrate_legacy(service)
        self._reader = GmailReader(service, user_id=gmail.user_id)
        self._draft_creator = GmailDraftCreator(service, user_id=gmail.user_id)
        self._sender = GmailSender(service, user_id=gmail.user_id)

    def _maybe_migrate_legacy(self, service) -> None:
        if self._token_store.active_token_path() is not None:
            return
        try:
            profile = service.users().getProfile(userId=self._settings.gmail.user_id).execute()
            email = (profile.get("emailAddress") or "").strip().lower()
        except Exception:  # noqa: BLE001 - migration is best-effort
            return
        if email:
            self._token_store.migrate_legacy(email)

    def list_messages(self, config: EmailListConfig) -> list[EmailMessage]:
        self._ensure_service()
        assert self._reader is not None
        read_config = GmailReadConfig(
            only_unread=config.only_unread,
            max_emails=config.max_emails,
            after_date=config.after_date,
            sender_filter=config.sender_filter,
            exclude_promotions=config.exclude_promotions,
            exclude_noreply=config.exclude_noreply,
        )
        return [_to_email_message(m) for m in self._reader.list_latest_unread(read_config)]

    def get_message(self, message_id: str) -> EmailMessage:
        self._ensure_service()
        assert self._reader is not None
        return _to_email_message(self._reader.get_message(message_id))

    def create_draft(self, message: EmailMessage, body: str) -> DraftResult:
        self._ensure_service()
        assert self._draft_creator is not None
        # GmailDraftCreator reads sender_email/subject/internet_message_id/
        # thread_id/id — all present on EmailMessage (same field names).
        result = self._draft_creator.create_draft(message, body)
        return DraftResult(
            draft_id=result.draft_id,
            message_id=result.message_id,
            thread_id=result.thread_id,
        )

    def send_reply(self, message: EmailMessage, body: str) -> SentResult:
        if not self._settings.enable_email_sending:
            raise PermissionError("Email sending is disabled (ENABLE_EMAIL_SENDING=false).")
        self._ensure_service()
        assert self._sender is not None
        result = self._sender.send_reply(message, body)
        return SentResult(message_id=result.message_id, thread_id=result.thread_id)

    def evaluate_replyability(self, message: EmailMessage) -> tuple[bool, str]:
        self._ensure_service()
        assert self._reader is not None
        return self._reader.evaluate_replyability(message, exclude_noreply=True)

    def health(self) -> tuple[str, str]:
        gmail = self._settings.gmail
        if not gmail.credentials_path.exists():
            return "error", "Google OAuth client not configured. Upload credentials.json in Settings → Gmail."
        if self._active_token_path() is None or not self._active_token_path().exists():
            return "not_authenticated", "Gmail not connected. Open Settings → Gmail to connect."
        try:
            self._ensure_service()
            assert self._reader is not None
            self._reader.service.users().getProfile(userId=gmail.user_id).execute()
            return "ok", "Gmail connected."
        except Exception as exc:  # noqa: BLE001 - health check must not raise
            return "error", f"Gmail check failed: {exc}"

    # --- Connection management (used by the web UI) --------------------------
    def reset(self) -> None:
        """Drop the cached service so the next call rebuilds with a new token."""
        self._reader = None
        self._draft_creator = None
        self._sender = None

    def connection_status(self) -> dict:
        """Non-sensitive connection status for the Settings → Gmail page.

        Never returns tokens or secrets — only the connected email, validity,
        and last-refresh timestamp.
        """
        gmail = self._settings.gmail
        creds_available = self.oauth.credentials_available()
        account = self._token_store.active_account(gmail.scopes)

        # Surface a legacy token.json (pre-migration) as connected too.
        if account is None and gmail.token_path.exists():
            try:
                creds = self._auth_manager().load_credentials()
            except Exception:  # noqa: BLE001
                creds = None
            if creds is not None:
                email = self.account_email() or "unknown"
                return {
                    "connected": True,
                    "email": email,
                    "valid": bool(creds.valid),
                    "expired": bool(creds.expired),
                    "last_refresh": None,
                    "scopes": list(creds.scopes or gmail.scopes),
                    "credentials_available": creds_available,
                    "accounts": self._token_store.list_emails(),
                    "send_scope": "https://www.googleapis.com/auth/gmail.send" in (creds.scopes or []),
                }

        if account is None:
            return {
                "connected": False,
                "email": None,
                "valid": False,
                "expired": False,
                "last_refresh": None,
                "scopes": [],
                "credentials_available": creds_available,
                "accounts": self._token_store.list_emails(),
                "send_scope": False,
            }
        return {
            "connected": account.valid or (account.expired and account.has_refresh_token),
            "email": account.email,
            "valid": account.valid,
            "expired": account.expired,
            "last_refresh": account.last_refresh,
            "scopes": account.scopes,
            "credentials_available": creds_available,
            "accounts": self._token_store.list_emails(),
            "send_scope": "https://www.googleapis.com/auth/gmail.send" in account.scopes,
        }

    def account_email(self) -> str | None:
        """Best-effort connected account email (used for legacy tokens)."""
        active = self._token_store.active_email()
        if active:
            return active
        try:
            self._ensure_service()
            assert self._reader is not None
            profile = self._reader.service.users().getProfile(
                userId=self._settings.gmail.user_id
            ).execute()
            return (profile.get("emailAddress") or "").strip().lower() or None
        except Exception:  # noqa: BLE001
            return None

    def begin_oauth(self) -> str:
        """Return the Google consent URL to open in the browser."""
        return self.oauth.authorization_url()

    def complete_oauth(self, *, code: str, state: str) -> str:
        """Finish OAuth: store the token and return the connected email."""
        creds, email = self.oauth.exchange_code(code=code, state=state)
        self._token_store.save(email, creds.to_json(), make_active=True)
        self.reset()
        return email

    def disconnect(self) -> None:
        """Remove the active account's token and clear the cached session."""
        email = self._token_store.active_email()
        if email:
            self._token_store.delete(email)
        # Also remove a lingering legacy token.json so reconnect is required.
        legacy = self._settings.gmail.token_path
        try:
            if legacy.exists():
                legacy.unlink()
        except OSError:
            pass
        self.reset()
