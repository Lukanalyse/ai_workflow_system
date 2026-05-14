from __future__ import annotations

import logging
from datetime import datetime

import streamlit as st

from app.auth.microsoft_auth import MicrosoftAuthManager
from app.config.settings import get_settings
from app.database.sqlite_manager import ProcessedEmailRecord, SQLiteManager
from app.email.clean_email import clean_email_body
from app.email.create_draft import OutlookDraftCreator
from app.email.filters import EmailFilterConfig
from app.email.read_emails import OutlookEmailReader
from app.llm.classify import classify_email
from app.llm.generate_reply import generate_reply_draft
from app.llm.llm_client import OpenAICompatibleClient
from app.llm.prompt_loader import PromptLoader
from app.llm.summarize import summarize_email


logger = logging.getLogger(__name__)

TONE_OPTIONS = ["formal", "academic", "concise", "friendly", "recruiter", "research"]


@st.cache_resource
def init_services() -> tuple[OutlookEmailReader, OutlookDraftCreator, OpenAICompatibleClient, PromptLoader, SQLiteManager]:
    settings = get_settings()
    auth = MicrosoftAuthManager(
        client_id=settings.microsoft.client_id,
        tenant_id=settings.microsoft.tenant_id,
        scopes=settings.microsoft.scopes,
        token_cache_path=settings.microsoft.token_cache_path,
    )
    return (
        OutlookEmailReader(auth, settings.graph_base_url),
        OutlookDraftCreator(auth, settings.graph_base_url),
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
        handlers=[
            logging.FileHandler(settings.log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    st.set_page_config(page_title="AI Email Workflow", layout="wide")
    st.title("AI-Assisted Email Drafting (Outlook)")

    reader, draft_creator, llm_client, prompt_loader, sqlite_manager = init_services()

    with st.sidebar:
        st.header("Filters")
        only_unread = st.checkbox("Only unread", value=settings.filters.only_unread)
        use_after_date = st.checkbox("Enable after-date filter", value=settings.filters.after_date is not None)
        after_date = st.date_input(
            "After date",
            value=settings.filters.after_date.date() if settings.filters.after_date else datetime.utcnow().date(),
        )
        keyword_input = st.text_input("Keywords (comma-separated)", value=",".join(settings.filters.keywords))
        whitelist_input = st.text_input(
            "Sender whitelist (comma-separated emails)",
            value=",".join(settings.filters.sender_whitelist),
        )
        blacklist_input = st.text_input(
            "Sender blacklist (comma-separated emails)",
            value=",".join(settings.filters.sender_blacklist),
        )
        exclude_newsletters = st.checkbox("Exclude newsletters", value=settings.filters.exclude_newsletters)
        exclude_automated = st.checkbox("Exclude automated emails", value=settings.filters.exclude_automated)
        tone = st.selectbox("Reply tone", options=TONE_OPTIONS, index=TONE_OPTIONS.index(settings.llm.default_tone) if settings.llm.default_tone in TONE_OPTIONS else 0)
        language = st.text_input("Reply language", value=settings.llm.default_language)
        max_count = st.slider("Max emails", min_value=1, max_value=100, value=settings.process_limit)

    filters = EmailFilterConfig.from_parts(
        only_unread=only_unread,
        after_date=datetime.combine(after_date, datetime.min.time()).astimezone() if use_after_date else None,
        sender_whitelist=[item.strip() for item in whitelist_input.split(",") if item.strip()],
        sender_blacklist=[item.strip() for item in blacklist_input.split(",") if item.strip()],
        keywords=[item.strip().lower() for item in keyword_input.split(",") if item.strip()],
        exclude_newsletters=exclude_newsletters,
        exclude_automated=exclude_automated,
    )

    if st.button("Load emails", type="primary"):
        emails = reader.fetch_inbox_messages(filters, limit=max_count)
        st.session_state["emails"] = emails

    emails = st.session_state.get("emails", [])
    st.subheader(f"Inbox candidates ({len(emails)})")
    for email in emails:
        with st.expander(f"{email.subject} — {email.sender_email}"):
            clean_body = clean_email_body(
                email.body_content, is_html=email.body_content_type.lower() == "html"
            )
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
                st.markdown("**Summary**")
                st.write(result["summary"])
                col1, col2, col3 = st.columns(3)
                col1.metric("Intent", result["intent"])
                col2.metric("Urgency", str(result["urgency"]))
                col3.metric("Confidence", f"{result['confidence']:.2f}")
                edited_draft = st.text_area("Draft reply", value=result["draft"], key=f"draft-{email.id}", height=220)

                action_col1, action_col2 = st.columns(2)
                if action_col1.button(f"Approve & create draft {email.id}", key=f"approve-{email.id}"):
                    draft_result = draft_creator.create_draft(email, edited_draft)
                    sqlite_manager.save_processed_email(
                        ProcessedEmailRecord(
                            message_id=email.id,
                            subject=email.subject,
                            sender=email.sender_email,
                            received_at=email.received_at.isoformat(),
                            summary=result["summary"],
                            intent_label=result["intent"],
                            urgency_score=result["urgency"],
                            draft_text=edited_draft,
                            confidence_score=float(result["confidence"]),
                            draft_id=draft_result.draft_id,
                            created_at=sqlite_manager.now_iso(),
                        )
                    )
                    st.success(f"Draft created: {draft_result.draft_id}")

                if action_col2.button(f"Reject {email.id}", key=f"reject-{email.id}"):
                    st.warning("Rejected. No draft created.")

    render_logs(str(settings.log_file))


if __name__ == "__main__":
    run()
