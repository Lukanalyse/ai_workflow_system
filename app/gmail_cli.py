from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from app.auth.gmail_auth import build_auth_manager
from app.config.settings import AppSettings, get_settings
from app.database.sqlite_manager import GmailProcessedEmailRecord, SQLiteManager
from app.email.clean_email import prepare_untrusted_email_for_llm
from app.email.gmail_draft_creator import GmailDraftCreator
from app.email.gmail_reader import GmailReadConfig, GmailReader
from app.llm.classify import classify_email
from app.llm.generate_reply import generate_reply_draft
from app.llm.llm_client import OpenAICompatibleClient
from app.llm.prompt_loader import PromptLoader
from app.llm.summarize import summarize_email
from app.security.startup_checks import sanitize_persisted_fields, validate_and_prepare_runtime

logger = logging.getLogger(__name__)
SUPPORTED_TONES = {"formal", "academic", "concise", "friendly", "recruiter", "research"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gmail-native AI email workflow (draft-only)")
    parser.add_argument("--max-emails", type=int, default=20, help="Max unread emails to inspect (hard-capped to 20)")
    parser.add_argument("--after-date", default="", help="Filter emails after date (YYYY-MM-DD)")
    parser.add_argument("--sender", default="", help="Filter sender address/domain")
    parser.add_argument(
        "--create-drafts",
        action="store_true",
        help="Force-enable Gmail draft creation for this run.",
    )
    parser.add_argument(
        "--no-drafts",
        action="store_true",
        help="Disable draft creation for this run (analyze-only mode).",
    )
    parser.add_argument("--validate-config", action="store_true", help="Validate local setup and exit")
    parser.add_argument("--tone", default="", help="Override tone")
    parser.add_argument("--language", default="", help="Override language")
    return parser.parse_args()


def _configure_logging(settings: AppSettings) -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.FileHandler(settings.log_file, encoding="utf-8"), logging.StreamHandler()],
    )


def _parse_after_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("Invalid --after-date. Expected format: YYYY-MM-DD") from exc


def main() -> None:
    args = _parse_args()
    settings = get_settings()
    _configure_logging(settings)
    try:
        from googleapiclient.discovery import build
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing dependency: google-api-python-client. Run `pip install -r requirements.txt`."
        ) from exc
    errors, warnings = validate_and_prepare_runtime(settings)
    for warning in warnings:
        logger.warning(warning)
    if errors:
        raise RuntimeError("Startup validation failed:\n- " + "\n- ".join(errors))
    if args.create_drafts and args.no_drafts:
        raise ValueError("Use either --create-drafts or --no-drafts, not both.")
    should_create_drafts = settings.create_drafts_default
    if args.create_drafts:
        should_create_drafts = True
    if args.no_drafts:
        should_create_drafts = False

    logger.info(
        "Startup mode: %s",
        "create_drafts" if should_create_drafts else "analyze_only (no draft creation)",
    )
    if args.validate_config:
        logger.info("Configuration validation successful.")
        return
    sqlite = SQLiteManager(settings.database.sqlite_path)

    auth = build_auth_manager(settings)
    service = build("gmail", "v1", credentials=auth.get_credentials())
    reader = GmailReader(service, user_id=settings.gmail.user_id)
    draft_creator = GmailDraftCreator(service, user_id=settings.gmail.user_id)
    llm_client = OpenAICompatibleClient(
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key,
        model=settings.llm.model,
    )
    prompt_loader = PromptLoader(settings.prompt_file)

    read_config = GmailReadConfig(
        only_unread=True,
        max_emails=max(1, min(args.max_emails, 20)),
        after_date=_parse_after_date(args.after_date),
        sender_filter=args.sender.strip() or None,
        exclude_promotions=True,
        exclude_noreply=True,
    )
    messages = reader.list_latest_unread(read_config)
    logger.info("Fetched %d unread Gmail messages (latest window)", len(messages))

    tone = args.tone.strip() or settings.llm.default_tone
    if tone not in SUPPORTED_TONES:
        tone = "formal"
    language = args.language.strip() or settings.llm.default_language
    for message in messages:
        now = sqlite.now_iso()
        already_processed, dedup_reason = sqlite.already_processed_gmail(message.id, message.thread_id)
        if already_processed:
            logger.info("Skipping message_id=%s reason=%s", message.id, dedup_reason)
            snippet, summary, draft_text = sanitize_persisted_fields(
                snippet=message.snippet,
                summary=None,
                draft_text=None,
                settings=settings,
            )
            sqlite.save_gmail_processed_email(
                GmailProcessedEmailRecord(
                    message_id=message.id,
                    thread_id=message.thread_id,
                    subject=message.subject,
                    sender=message.sender_email,
                    received_at=message.received_at.isoformat(),
                    snippet=snippet,
                    processed_status="skipped",
                    draft_created=False,
                    draft_id=None,
                    skip_reason=dedup_reason,
                    summary=None,
                    intent_label=None,
                    urgency_score=None,
                    confidence_score=None,
                    draft_text=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            continue

        replyable, reason = reader.evaluate_replyability(message, exclude_noreply=True)
        if not replyable:
            logger.info("Skipping non-replyable message_id=%s reason=%s", message.id, reason)
            snippet, summary, draft_text = sanitize_persisted_fields(
                snippet=message.snippet,
                summary=None,
                draft_text=None,
                settings=settings,
            )
            sqlite.save_gmail_processed_email(
                GmailProcessedEmailRecord(
                    message_id=message.id,
                    thread_id=message.thread_id,
                    subject=message.subject,
                    sender=message.sender_email,
                    received_at=message.received_at.isoformat(),
                    snippet=snippet,
                    processed_status="skipped",
                    draft_created=False,
                    draft_id=None,
                    skip_reason=reason,
                    summary=None,
                    intent_label=None,
                    urgency_score=None,
                    confidence_score=None,
                    draft_text=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            continue

        llm_input = prepare_untrusted_email_for_llm(
            message.body_text or message.snippet,
            is_html=False,
            max_chars=settings.llm.max_input_chars,
            attachment_names=message.attachment_names,
        )
        draft_id: str | None = None
        processed_status = "processed"
        skip_reason: str | None = None
        summary: str | None = None
        intent_label: str | None = None
        urgency_score: int | None = None
        confidence: float | None = None
        draft_text: str | None = None
        try:
            summary = summarize_email(
                llm_client,
                prompt_loader,
                subject=message.subject,
                sender=message.sender_email,
                received_at=message.received_at.isoformat(),
                body=llm_input,
                temperature=settings.llm.temperature,
                max_tokens=settings.llm.max_tokens,
            )
            classification = classify_email(
                llm_client,
                prompt_loader,
                subject=message.subject,
                sender=message.sender_email,
                body=llm_input,
                temperature=settings.llm.temperature,
                max_tokens=settings.llm.max_tokens,
            )
            intent_label = classification.intent_label
            urgency_score = classification.urgency_score
            confidence = classification.confidence
            draft_text = generate_reply_draft(
                llm_client,
                prompt_loader,
                subject=message.subject,
                sender=message.sender_email,
                body=llm_input,
                tone=tone,
                language=language,
                temperature=settings.llm.temperature,
                max_tokens=settings.llm.max_tokens,
            )
            if should_create_drafts:
                draft_result = draft_creator.create_draft(message, draft_text)
                draft_id = draft_result.draft_id
            else:
                processed_status = "analyzed_only"
        except Exception:
            logger.exception("Failed processing Gmail message_id=%s", message.id)
            processed_status = "failed"
            skip_reason = "processing_exception"

        updated_at = sqlite.now_iso()
        snippet_db, summary_db, draft_db = sanitize_persisted_fields(
            snippet=message.snippet,
            summary=summary,
            draft_text=draft_text,
            settings=settings,
        )
        sqlite.save_gmail_processed_email(
            GmailProcessedEmailRecord(
                message_id=message.id,
                thread_id=message.thread_id,
                subject=message.subject,
                sender=message.sender_email,
                received_at=message.received_at.isoformat(),
                snippet=snippet_db,
                processed_status=processed_status,
                draft_created=bool(draft_id),
                draft_id=draft_id,
                skip_reason=skip_reason,
                summary=summary_db,
                intent_label=intent_label,
                urgency_score=urgency_score,
                confidence_score=confidence,
                draft_text=draft_db,
                created_at=now,
                updated_at=updated_at,
            )
        )
        logger.info(
            "Processed Gmail message_id=%s status=%s draft_created=%s",
            message.id,
            processed_status,
            bool(draft_id),
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(f"Startup error: {exc}") from exc
