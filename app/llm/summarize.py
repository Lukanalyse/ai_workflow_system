from __future__ import annotations

from app.llm.llm_client import OpenAICompatibleClient
from app.llm.prompt_loader import PromptLoader


def summarize_email(
    llm_client: OpenAICompatibleClient,
    prompt_loader: PromptLoader,
    *,
    subject: str,
    sender: str,
    received_at: str,
    body: str,
    temperature: float,
    max_tokens: int,
) -> str:
    prompt = prompt_loader.get("summary")
    user_prompt = prompt["user_template"].format(
        subject=subject, sender=sender, received_at=received_at, body=body
    )
    return llm_client.chat_completion(
        system_prompt=prompt["system"],
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    ).content

