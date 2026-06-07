from __future__ import annotations

from app.agents.base_agent import BaseAgent
from app.llm.base import LLMResult


class SummarizeAgent(BaseAgent):
    """Produces a concise summary of an incoming email."""

    name = "summarize"
    prompt_key = "summary"

    def run(self, *, subject: str, sender: str, received_at: str, body: str) -> LLMResult:
        return self._complete(subject=subject, sender=sender, received_at=received_at, body=body)
