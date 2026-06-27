from __future__ import annotations

import logging
import threading

from app.auth.gmail_auth import GmailAuthManager, build_auth_manager
from app.auth.oauth_flow import GmailOAuthFlow
from app.auth.token_store import TokenStore
from app.config.settings import GMAIL_MODIFY_SCOPE, AppSettings
from app.email.gmail_actions import GmailActions
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
        attachments=list(msg.attachments),
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
        self._actions: GmailActions | None = None
        # Guards lazy service construction so a concurrent first-access burst
        # builds the client exactly once.
        self._service_lock = threading.Lock()
        gmail = settings.gmail
        self._token_store = TokenStore(gmail.tokens_dir, legacy_token_path=gmail.token_path)
        self.oauth = GmailOAuthFlow(
            credentials_path=gmail.credentials_path,
            # Request gmail.modify at consent so a (re)connect enables mailbox
            # actions; the validity gate still uses the narrower gmail.scopes.
            scopes=gmail.oauth_scopes,
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
        with self._service_lock:
            if self._reader is not None:  # another thread built it while we waited
                return
            try:
                from app.email.gmail_http import build_gmail_service
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
            # Thread-safe transport: every request uses a thread-local HTTP
            # client, so concurrent calls never share a TLS connection.
            service, http_factory = build_gmail_service(creds)
            # Adopt a legacy token.json into the new store the first time we use
            # it (its account email isn't known until we can call the API).
            self._maybe_migrate_legacy(service)
            self._reader = GmailReader(service, user_id=gmail.user_id, http_factory=http_factory)
            self._draft_creator = GmailDraftCreator(service, user_id=gmail.user_id)
            self._sender = GmailSender(service, user_id=gmail.user_id)
            self._actions = GmailActions(service, user_id=gmail.user_id, http_factory=http_factory)

    def _maybe_migrate_legacy(self, service) -> None:
        if self._token_store.active_token_path() is not None:
            return
        try:
            from app.email.gmail_http import execute as gmail_execute

            profile = gmail_execute(
                service.users().getProfile(userId=self._settings.gmail.user_id),
                op="users.getProfile",
            )
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
            status=config.status,
        )
        return [_to_email_message(m) for m in self._reader.list_latest_unread(read_config)]

    def get_message(self, message_id: str) -> EmailMessage:
        self._ensure_service()
        assert self._reader is not None
        return _to_email_message(self._reader.get_message(message_id))

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        self._ensure_service()
        assert self._reader is not None
        return self._reader.get_attachment(message_id, attachment_id)

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

    # --- Mailbox mutations (require gmail.modify) ----------------------------
    def has_modify_scope(self) -> bool:
        """True when the active credentials were granted gmail.modify."""
        gmail = self._settings.gmail
        account = self._token_store.active_account(gmail.scopes)
        if account is not None:
            return GMAIL_MODIFY_SCOPE in account.scopes
        if gmail.token_path.exists():
            try:
                creds = self._auth_manager().load_credentials()
                return creds is not None and GMAIL_MODIFY_SCOPE in (creds.scopes or [])
            except Exception:  # noqa: BLE001
                return False
        return False

    def _require_modify_scope(self) -> None:
        if not self.has_modify_scope():
            raise PermissionError(
                "Gmail mailbox actions need an extra permission. Open Settings → "
                "Gmail and reconnect Gmail to enable archive, labels and read-state changes."
            )

    def modify_labels(
        self,
        message_ids: list[str],
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> int:
        self._require_modify_scope()
        self._ensure_service()
        assert self._actions is not None
        return self._actions.batch_modify(message_ids, add_label_ids=add, remove_label_ids=remove)

    def list_labels(self) -> list:
        self._require_modify_scope()
        self._ensure_service()
        assert self._actions is not None
        return self._actions.list_labels()

    def label_counts(self, label_id: str) -> tuple[int, int]:
        self._ensure_service()
        assert self._reader is not None
        return self._reader.get_label_counts(label_id)

    def label_counts_many(self, label_ids: list[str]) -> dict[str, tuple[int, int]]:
        self._ensure_service()
        assert self._reader is not None
        return self._reader.get_label_counts_many(label_ids)

    def list_label_messages(
        self, label_id: str, *, page_size: int = 25, page_token: str | None = None
    ) -> tuple[list[EmailMessage], str | None]:
        self._ensure_service()
        assert self._reader is not None
        messages, next_token = self._reader.list_by_label(
            label_id, page_size=page_size, page_token=page_token
        )
        return [_to_email_message(m) for m in messages], next_token

    def create_label(self, name: str):
        self._require_modify_scope()
        self._ensure_service()
        assert self._actions is not None
        return self._actions.create_label(name)

    def health(self) -> tuple[str, str]:
        gmail = self._settings.gmail
        if not gmail.credentials_path.exists():
            return "error", "Google OAuth client not configured. Upload credentials.json in Settings → Gmail."
        if self._active_token_path() is None or not self._active_token_path().exists():
            return "not_authenticated", "Gmail not connected. Open Settings → Gmail to connect."
        try:
            self._ensure_service()
            assert self._reader is not None
            from app.email.gmail_http import execute as gmail_execute

            gmail_execute(
                self._reader.service.users().getProfile(userId=gmail.user_id),
                op="users.getProfile",
            )
            return "ok", "Gmail connected."
        except Exception as exc:  # noqa: BLE001 - health check must not raise
            return "error", f"Gmail check failed: {exc}"

    # --- Connection management (used by the web UI) --------------------------
    def reset(self) -> None:
        """Drop the cached service so the next call rebuilds with a new token."""
        self._reader = None
        self._draft_creator = None
        self._sender = None
        self._actions = None

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
                    "modify_scope": GMAIL_MODIFY_SCOPE in (creds.scopes or []),
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
                "modify_scope": False,
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
            "modify_scope": GMAIL_MODIFY_SCOPE in account.scopes,
        }

    def account_email(self) -> str | None:
        """Best-effort connected account email (used for legacy tokens)."""
        active = self._token_store.active_email()
        if active:
            return active
        try:
            self._ensure_service()
            assert self._reader is not None
            from app.email.gmail_http import execute as gmail_execute

            profile = gmail_execute(
                self._reader.service.users().getProfile(userId=self._settings.gmail.user_id),
                op="users.getProfile",
            )
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
