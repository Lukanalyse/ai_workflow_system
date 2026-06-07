from __future__ import annotations

import logging

from app.llm.base import LLMClient, LLMResult

logger = logging.getLogger(__name__)


class AnthropicClient(LLMClient):
    """Claude client built on the official Anthropic SDK (Messages API).

    Note: Opus 4.7+/4.8 reject sampling params (temperature/top_p/top_k) and
    `budget_tokens`, so this client steers purely via the prompt and never
    sends those fields — keeping it valid across every current Claude model.
    """

    provider = "anthropic"

    def __init__(self, *, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self._client = None  # lazy: avoid importing the SDK until first use

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Missing dependency: anthropic. Run `pip install -r requirements.txt`."
                ) from exc
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> LLMResult:
        client = self._get_client()
        logger.debug("Sending Anthropic message request model=%s", self.model)
        response = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        usage = getattr(response, "usage", None)
        return LLMResult(
            text="".join(parts).strip(),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            model=self.model,
            provider=self.provider,
        )
