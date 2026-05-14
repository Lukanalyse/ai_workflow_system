from __future__ import annotations

import logging
from datetime import datetime

import streamlit as st
from googleapiclient.discovery import build

from app.auth.gmail_auth import GmailAuthManager
from app.config.settings import get_settings
from app.database.sqlite_manager import GmailProcessedEmailRecord, SQLiteManager
from app.email.clean_email import clean_email_body
from app.email.gmail_draft_creator import GmailDraftCreator
from app.email.gmail_reader import GmailReadConfig, GmailReader
from app.llm.classify import classify_email
from app.llm.generate_reply import generate_reply_draft
from app.llm.llm_client import OpenAICompatibleClient
from app.llm.prompt_loader import PromptLoader
from app.llm.summarize import summarize_email

logger = logging.getLogger(__name__)
TONE_OPTIONS = ["formal", "academic", "concise", "friendly", "recruiter", "research"]


@st.cache_resource
def init_services() -> tuple[GmailReader, GmailDraftCreator, OpenAICompatibleClient, PromptLoader, SQLiteManager]:
    settings = get_settings()
    auth = GmailAuthManager(
        credentials_path=settings.gmail.credentials_path,
        token_path=settings.gmail.token_path,
        scopes=settings.gmail.scopes,
    )
    service = build("gmail", "v1", credentials=auth.get_credentials())
    return (
        GmailReader(service, user_id=settings.gmail.user_id),
        GmailDraftCreator(service, user_id=settings.gmail.user_id),
        OpenAICompatibleClient(
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key,
            model=settings.llm.model,
        ),
        PromptLoader(settings.prompt_file),
        SQLiteManager(settings.database.sqlite_path),
    )


def render_logs(log_file: str) -> None:
    st.subheader("Logs")
    try:
        with open(log_file, "r", encoding="utf-8") as handle:
            lines = handle.readlines()[-150:]
        st.code("".join(lines), language="text")
    except FileNotFoundError:
        st.info("No logs yet.")


def run() -> None:
    settings = get_settings()
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.FileHandler(settings.log_file, encoding="utf-8"), logging.StreamHandler()],
    )

    st.set_page_config(page_title="Gmail AI Email Assistant", layout="wide")
    st.title("Gmail AI Email Assistant")
    st.caption("Draft-only mode: emails are never auto-sent.")

    reader, draft_creator, llm_client, prompt_loader, sqlite_manager = init_services()

    with st.sidebar:
        st.header("Filters")
        only_unread = st.checkbox("Unread only", value=settings.filters.only_unread)
        use_after_date = st.checkbox("Enable after-date filter", value=settings.filters.after_date is not None)
        after_date = st.date_input(
            "After date",
            value=settings.filters.after_date.date() if settings.filters.after_date else datetime.utcnow().date(),
        )
        sender_filter = st.text_input("Sender filter (email/domain)", value=settings.filters.sender_filter)
        exclude_promotions = st.checkbox("Exclude promotions/social", value=settings.filters.exclude_promotions)
        exclude_noreply = st.checkbox("Exclude noreply/automated", value=settings.filters.exclude_noreply)
        max_count = st.slider(
            "Max emails",
            min_value=1,
            max_value=20,
            value=min(max(1, settings.filters.max_emails), 20),
        )
        tone = st.selectbox(
            "Reply tone",
            options=TONE_OPTIONS,
            index=TONE_OPTIONS.index(settings.llm.default_tone) if settings.llm.default_tone in TONE_OPTIONS else 0,
        )
        language = st.text_input("Reply language", value=settings.llm.default_language)

    read_config = GmailReadConfig(
        only_unread=only_unread,
        max_emails=max_count,
        after_date=datetime.combine(after_date, datetime.min.time()).astimezone() if use_after_date else None,
        sender_filter=sender_filter.strip() or None,
        exclude_promotions=exclude_promotions,
        exclude_noreply=exclude_noreply,
    )

    if st.button("Load Gmail emails", type="primary"):
        emails = reader.list_latest_unread(read_config)
        st.session_state["emails"] = emails

    emails = st.session_state.get("emails", [])
    st.subheader(f"Gmail candidates ({len(emails)})")
    for email in emails:
        with st.expander(f"{email.subject} — {email.sender_email}"):
            already_seen, dedup_reason = sqlite_manager.already_processed_gmail(email.id, email.thread_id)
            replyable, reply_reason = reader.evaluate_replyability(email, exclude_noreply=exclude_noreply)
            clean_body = clean_email_body(email.body_text or email.snippet, is_html=False)

            st.markdown(f"**Snippet:** {email.snippet}")
            st.markdown(f"**Dedup:** {'skip' if already_seen else 'new'} ({dedup_reason or 'n/a'})")
            st.markdown(f"**Replyability:** {'eligible' if replyable else 'excluded'} ({reply_reason})")
            if email.has_attachments:
                st.markdown("**Attachments:** detected and acknowledged (content is not read).")
            st.write(clean_body[:2000] + ("..." if len(clean_body) > 2000 else ""))

            if st.button(f"Analyze {email.id}", key=f"analyze-{email.id}"):
                summary = summarize_email(
                    llm_client,
                    prompt_loader,
                    subject=email.subject,
                    sender=email.sender_email,
                    received_at=email.received_at.isoformat(),
                    body=clean_body,
                    temperature=settings.llm.temperature,
                    max_tokens=settings.llm.max_tokens,
                )
                classification = classify_email(
                    llm_client,
                    prompt_loader,
                    subject=email.subject,
                    sender=email.sender_email,
                    body=clean_body,
                    temperature=settings.llm.temperature,
                    max_tokens=settings.llm.max_tokens,
                )
                draft = generate_reply_draft(
                    llm_client,
                    prompt_loader,
                    subject=email.subject,
                    sender=email.sender_email,
                    body=clean_body,
                    tone=tone,
                    language=language,
                    temperature=settings.llm.temperature,
                    max_tokens=settings.llm.max_tokens,
                )
                st.session_state[f"result-{email.id}"] = {
                    "summary": summary,
                    "intent": classification.intent_label,
                    "urgency": classification.urgency_score,
                    "confidence": classification.confidence,
                    "draft": draft,
                }

            result = st.session_state.get(f"result-{email.id}")
            if result:
                st.markdown("**AI Summary**")
                st.write(result["summary"])
                col1, col2, col3 = st.columns(3)
                col1.metric("Intent", result["intent"])
                col2.metric("Urgency", str(result["urgency"]))
                col3.metric("Confidence", f"{result['confidence']:.2f}")
                edited_draft = st.text_area("Draft reply", value=result["draft"], key=f"draft-{email.id}", height=220)

                can_create = not already_seen and replyable
                if st.button(f"Create Gmail draft {email.id}", key=f"approve-{email.id}", disabled=not can_create):
                    draft_result = draft_creator.create_draft(email, edited_draft)
                    now = sqlite_manager.now_iso()
                    sqlite_manager.save_gmail_processed_email(
                        GmailProcessedEmailRecord(
                            message_id=email.id,
                            thread_id=email.thread_id,
                            subject=email.subject,
                            sender=email.sender_email,
                            received_at=email.received_at.isoformat(),
                            snippet=email.snippet,
                            processed_status="processed",
                            draft_created=True,
                            draft_id=draft_result.draft_id,
                            skip_reason=None,
                            summary=result["summary"],
                            intent_label=result["intent"],
                            urgency_score=result["urgency"],
                            confidence_score=float(result["confidence"]),
                            draft_text=edited_draft,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    st.success(f"Gmail draft created: {draft_result.draft_id}")

    render_logs(str(settings.log_file))


if __name__ == "__main__":
    run()

