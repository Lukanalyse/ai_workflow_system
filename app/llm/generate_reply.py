from __future__ import annotations

from app.llm.llm_client import OpenAICompatibleClient
from app.llm.prompt_loader import PromptLoader


def generate_reply_draft(
    llm_client: OpenAICompatibleClient,
    prompt_loader: PromptLoader,
    *,
    subject: str,
    sender: str,
    body: str,
    tone: str,
    language: str,
    temperature: float,
    max_tokens: int,
) -> str:
    prompt = prompt_loader.get("generate_reply")
    user_prompt = prompt["user_template"].format(
        subject=subject,
        sender=sender,
        body=body,
        tone=tone,
        language=language,
    )
    return llm_client.chat_completion(
        system_prompt=prompt["system"],
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    ).content

