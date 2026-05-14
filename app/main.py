from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from app.auth.microsoft_auth import MicrosoftAuthManager
from app.config.settings import AppSettings, get_settings
from app.database.sqlite_manager import ProcessedEmailRecord, SQLiteManager
from app.email.clean_email import clean_email_body
from app.email.create_draft import OutlookDraftCreator
from app.email.filters import EmailFilterConfig
from app.email.read_emails import EmailMessage, OutlookEmailReader
from app.llm.classify import ClassificationResult, classify_email
from app.llm.generate_reply import generate_reply_draft
from app.llm.llm_client import OpenAICompatibleClient
from app.llm.prompt_loader import PromptLoader
from app.llm.summarize import summarize_email


logger = logging.getLogger(__name__)


SUPPORTED_TONES = {"formal", "academic", "concise", "friendly", "recruiter", "research"}


@dataclass(slots=True)
class ProcessingOutput:
    message_id: str
    subject: str
    sender: str
    summary: str
    intent: str
    urgency: int
    confidence: float
    draft_id: str | None
    draft_text: str


def configure_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def _build_filter_config(settings: AppSettings) -> EmailFilterConfig:
    return EmailFilterConfig.from_parts(
        only_unread=settings.filters.only_unread,
        after_date=settings.filters.after_date,
        sender_whitelist=settings.filters.sender_whitelist,
        sender_blacklist=settings.filters.sender_blacklist,
        keywords=settings.filters.keywords,
        exclude_newsletters=settings.filters.exclude_newsletters,
        exclude_automated=settings.filters.exclude_automated,
    )


def _init_services(settings: AppSettings) -> tuple[
    OutlookEmailReader,
    OutlookDraftCreator,
    OpenAICompatibleClient,
    PromptLoader,
    SQLiteManager,
]:
    auth = MicrosoftAuthManager(
        client_id=settings.microsoft.client_id,
        tenant_id=settings.microsoft.tenant_id,
        scopes=settings.microsoft.scopes,
        token_cache_path=settings.microsoft.token_cache_path,
    )
    email_reader = OutlookEmailReader(auth, settings.graph_base_url)
    draft_creator = OutlookDraftCreator(auth, settings.graph_base_url)
    llm_client = OpenAICompatibleClient(
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key,
        model=settings.llm.model,
    )
    prompt_loader = PromptLoader(settings.prompt_file)
    sqlite_manager = SQLiteManager(settings.database.sqlite_path)
    return email_reader, draft_creator, llm_client, prompt_loader, sqlite_manager


def process_email(
    email: EmailMessage,
    *,
    llm_client: OpenAICompatibleClient,
    prompt_loader: PromptLoader,
    draft_creator: OutlookDraftCreator,
    sqlite_manager: SQLiteManager,
    tone: str,
    language: str,
    llm_temperature: float,
    llm_max_tokens: int,
    create_drafts: bool,
) -> ProcessingOutput:
    body = clean_email_body(email.body_content, is_html=email.body_content_type.lower() == "html")
    summary = summarize_email(
        llm_client,
        prompt_loader,
        subject=email.subject,
        sender=email.sender_email,
        received_at=email.received_at.isoformat(),
        body=body,
        temperature=llm_temperature,
        max_tokens=llm_max_tokens,
    )
    classification: ClassificationResult = classify_email(
        llm_client,
        prompt_loader,
        subject=email.subject,
        sender=email.sender_email,
        body=body,
        temperature=llm_temperature,
        max_tokens=llm_max_tokens,
    )
    draft_text = generate_reply_draft(
        llm_client,
        prompt_loader,
        subject=email.subject,
        sender=email.sender_email,
        body=body,
        tone=tone,
        language=language,
        temperature=llm_temperature,
        max_tokens=llm_max_tokens,
    )
    draft_id: str | None = None
    if create_drafts:
        draft = draft_creator.create_draft(email, draft_text)
        draft_id = draft.draft_id

    sqlite_manager.save_processed_email(
        ProcessedEmailRecord(
            message_id=email.id,
            subject=email.subject,
            sender=email.sender_email,
            received_at=email.received_at.isoformat(),
            summary=summary,
            intent_label=classification.intent_label,
            urgency_score=classification.urgency_score,
            draft_text=draft_text,
            confidence_score=classification.confidence,
            draft_id=draft_id,
            created_at=sqlite_manager.now_iso(),
        )
    )
    return ProcessingOutput(
        message_id=email.id,
        subject=email.subject,
        sender=email.sender_email,
        summary=summary,
        intent=classification.intent_label,
        urgency=classification.urgency_score,
        confidence=classification.confidence,
        draft_id=draft_id,
        draft_text=draft_text,
    )


def process_inbox_once(settings: AppSettings, *, create_drafts: bool = True) -> list[ProcessingOutput]:
    reader, draft_creator, llm_client, prompt_loader, sqlite_manager = _init_services(settings)
    filters = _build_filter_config(settings)
    emails = reader.fetch_inbox_messages(filters, limit=settings.process_limit)
    logger.info("Fetched %d candidate emails", len(emails))
    results: list[ProcessingOutput] = []
    tone = settings.llm.default_tone if settings.llm.default_tone in SUPPORTED_TONES else "formal"
    for email in emails:
        if sqlite_manager.already_processed(email.id):
            logger.info("Skipping already processed email: %s", email.id)
            continue
        try:
            output = process_email(
                email,
                llm_client=llm_client,
                prompt_loader=prompt_loader,
                draft_creator=draft_creator,
                sqlite_manager=sqlite_manager,
                tone=tone,
                language=settings.llm.default_language,
                llm_temperature=settings.llm.temperature,
                llm_max_tokens=settings.llm.max_tokens,
                create_drafts=create_drafts,
            )
            logger.info("Processed email %s with intent=%s", email.id, output.intent)
            results.append(output)
        except Exception:
            logger.exception("Failed processing email %s", email.id)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-assisted Outlook email workflow")
    parser.add_argument(
        "--no-drafts",
        action="store_true",
        help="Generate analysis and reply text but do not create Outlook drafts.",
    )
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_file)
    results = process_inbox_once(settings, create_drafts=not args.no_drafts)
    logger.info("Run complete. Processed emails: %d", len(results))
    for row in results:
        logger.info(
            "message=%s | subject=%s | intent=%s | urgency=%d | draft_id=%s",
            row.message_id,
            row.subject,
            row.intent,
            row.urgency,
            row.draft_id,
        )


if __name__ == "__main__":
    main()

