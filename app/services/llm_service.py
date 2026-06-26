from __future__ import annotations

import logging

from app.agents.draft_agent import DraftAgent
from app.agents.summarize_agent import SummarizeAgent
from app.config.settings import AppSettings
from app.config.user_config import UserConfig
from app.database.sqlite_manager import SQLiteManager, UsageEventRecord
from app.email.clean_email import prepare_untrusted_email_for_llm
from app.llm.base import LLMResult
from app.llm.email_analysis import EmailAnalysis, build_analyze_prompts, parse_analysis
from app.llm.prompt_loader import PromptLoader
from app.llm.provider import build_llm_client
from app.providers.base import EmailMessage
from app.services.cost_service import CostService

logger = logging.getLogger(__name__)

SUPPORTED_TONES = {
    "professional",
    "friendly",
    "formal",
    "concise",
    "detailed",
    "academic",
    "recruiter",
    "research",
}

# Map the user-facing language codes to an instruction the draft prompt can use.
_LANGUAGE_INSTRUCTIONS = {
    "auto": "the same language as the original email",
    "fr": "French",
    "en": "English",
}


class LLMService:
    """Owns the LLM client + agents and exposes high-level email operations.

    This is the single place provider selection and agent wiring happen, so
    the web/CLI layers never touch model details. Token usage from every call
    is recorded here (one place) via CostService + SQLiteManager.
    """

    def __init__(
        self,
        settings: AppSettings,
        *,
        sqlite: SQLiteManager | None = None,
        cost_service: CostService | None = None,
        user_config: UserConfig | None = None,
    ) -> None:
        self._settings = settings
        self._client = build_llm_client(settings)
        self._sqlite = sqlite
        self._cost = cost_service
        self._user_config = user_config or UserConfig()
        prompt_loader = PromptLoader(settings.prompt_file)
        self._summarize_agent = SummarizeAgent(
            self._client, prompt_loader, max_tokens=settings.llm.max_tokens
        )
        self._draft_agent = DraftAgent(
            self._client, prompt_loader, max_tokens=settings.llm.max_tokens
        )

    def _prepare_body(self, email: EmailMessage) -> str:
        return prepare_untrusted_email_for_llm(
            email.body_text or email.snippet,
            is_html=False,
            max_chars=self._settings.llm.max_input_chars,
            attachment_names=email.attachment_names,
        )

    def _record(
        self, result: LLMResult, *, operation: str, email_id: str | None, run_id: str | None = None
    ) -> None:
        # Usage recording must never break the user-facing request path.
        if self._sqlite is None or self._cost is None:
            return
        try:
            cost = self._cost.estimate(
                result.provider, result.model, result.input_tokens, result.output_tokens
            )
            self._sqlite.record_usage_event(
                UsageEventRecord(
                    timestamp=self._sqlite.now_iso(),
                    provider=result.provider,
                    model=result.model,
                    operation=operation,
                    email_message_id=email_id,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    total_tokens=result.total_tokens,
                    estimated_cost=cost,
                    currency=self._cost.currency,
                    run_id=run_id,
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record usage event (operation=%s)", operation)

    def summarize(self, email: EmailMessage, *, run_id: str | None = None) -> str:
        result = self._summarize_agent.run(
            subject=email.subject,
            sender=email.sender_email,
            received_at=email.received_at.isoformat(),
            body=self._prepare_body(email),
        )
        self._record(result, operation="summarize", email_id=email.id, run_id=run_id)
        return result.text

    def analyze_email(self, email: EmailMessage, *, run_id: str | None = None) -> EmailAnalysis:
        """One LLM call producing the unified understanding-layer structure.

        The body is wrapped as untrusted input (same as every other call) and
        token usage is recorded under the ``analyze`` operation.
        """
        system, user = build_analyze_prompts(
            subject=email.subject,
            sender=email.sender_email,
            attachments=email.attachment_names,
            body=self._prepare_body(email),
        )
        result = self._client.complete(
            system_prompt=system, user_prompt=user, max_tokens=self._settings.llm.max_tokens
        )
        self._record(result, operation="analyze", email_id=email.id, run_id=run_id)
        return parse_analysis(result.text, model=result.model)

    def _resolve_language(self, language: str | None) -> str:
        code = (language or self._user_config.default_language or "auto").strip().lower()
        return _LANGUAGE_INSTRUCTIONS.get(code, language or "English")

    def generate_draft(
        self,
        email: EmailMessage,
        *,
        tone: str | None = None,
        language: str | None = None,
        run_id: str | None = None,
    ) -> str:
        resolved_tone = (tone or self._user_config.default_tone or "professional").strip().lower()
        if resolved_tone not in SUPPORTED_TONES:
            resolved_tone = self._user_config.default_tone or "professional"
        resolved_language = self._resolve_language(language)
        result = self._draft_agent.run(
            subject=email.subject,
            sender=email.sender_email,
            body=self._prepare_body(email),
            tone=resolved_tone,
            language=resolved_language,
            custom_instructions=self._user_config.custom_prompt,
        )
        self._record(result, operation="draft", email_id=email.id, run_id=run_id)
        return self._append_signature(result.text)

    def _append_signature(self, text: str) -> str:
        signature = (self._user_config.signature or "").strip()
        if not signature:
            return text
        if signature in text:
            return text
        return f"{text.rstrip()}\n\n{signature}"

    def health(self) -> tuple[str, str]:
        llm = self._settings.llm
        placeholder = llm.api_key.strip().lower() in {
            "",
            "your-openai-api-key",
            "changeme",
            "replace-me",
            "set-me",
        }
        if placeholder:
            return "not_configured", f"No API key set for provider '{llm.provider}'."
        return "configured", f"Provider '{llm.provider}', model '{llm.model}'."
