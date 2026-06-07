from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.llm.base import LLMClient, LLMResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LLMResponse:
    content: str
    model: str
    raw: dict[str, Any]


class OpenAICompatibleClient(LLMClient):
    """Minimal client for OpenAI-compatible Chat Completions APIs."""

    provider = "openai"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.2,
        provider: str = "openai",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.provider = provider

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> LLMResult:
        response = self.chat_completion(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=self.temperature,
            max_tokens=max_tokens,
        )
        usage = response.raw.get("usage", {}) or {}
        return LLMResult(
            text=response.content,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            model=response.model,
            provider=self.provider,
        )

    def chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 700,
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=60.0) as client:
            logger.debug("Sending chat completion request to %s", self.base_url)
            response = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        content = data["choices"][0]["message"]["content"]
        return LLMResponse(content=str(content).strip(), model=self.model, raw=data)

