from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class LLMResult:
    """Provider-agnostic result of a single completion.

    Carries token usage alongside the text so cost tracking never has to reach
    back into provider-specific payloads. New providers fill these fields from
    their own response shape (e.g. OpenAI `usage`, Anthropic `response.usage`).
    """

    text: str
    input_tokens: int
    output_tokens: int
    model: str
    provider: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMClient(ABC):
    """Provider-agnostic chat interface used by the agents.

    Implementations wrap a concrete provider (OpenAI-compatible, Anthropic).
    Agents only depend on this surface, so adding a provider never touches
    agent or service code.
    """

    model: str
    provider: str = "unknown"

    @abstractmethod
    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> LLMResult:
        """Return the assistant text and token usage for a single-turn prompt."""
