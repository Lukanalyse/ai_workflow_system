from __future__ import annotations

from app.agents.base_agent import BaseAgent
from app.llm.base import LLMResult


class DraftAgent(BaseAgent):
    """Generates a suggested reply draft for an incoming email."""

    name = "draft"
    prompt_key = "generate_reply"

    def run(
        self,
        *,
        subject: str,
        sender: str,
        body: str,
        tone: str,
        language: str,
        custom_instructions: str = "",
    ) -> LLMResult:
        return self._complete(
            system_extra=custom_instructions,
            subject=subject,
            sender=sender,
            body=body,
            tone=tone,
            language=language,
        )
