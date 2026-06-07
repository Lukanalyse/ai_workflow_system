from __future__ import annotations

from app.config.settings import AppSettings
from app.llm.anthropic_client import AnthropicClient
from app.llm.base import LLMClient
from app.llm.llm_client import OpenAICompatibleClient


def build_llm_client(settings: AppSettings) -> LLMClient:
    """Return the LLM client for the provider resolved in settings.

    Provider selection happens in AppSettings (explicit LLM_PROVIDER, else
    whichever API key is set). This just constructs the matching client.
    """
    llm = settings.llm
    if llm.provider == "anthropic":
        return AnthropicClient(api_key=llm.api_key, model=llm.model)
    return OpenAICompatibleClient(
        base_url=llm.base_url,
        api_key=llm.api_key,
        model=llm.model,
        temperature=llm.temperature,
        provider=llm.provider or "openai",
    )
