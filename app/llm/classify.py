from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.llm.llm_client import OpenAICompatibleClient
from app.llm.prompt_loader import PromptLoader


@dataclass(slots=True)
class ClassificationResult:
    intent_label: str
    intent_reason: str
    urgency_score: int
    urgency_reason: str
    confidence: float


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("Model output did not include JSON")
    return json.loads(match.group(0))


def classify_email(
    llm_client: OpenAICompatibleClient,
    prompt_loader: PromptLoader,
    *,
    subject: str,
    sender: str,
    body: str,
    temperature: float,
    max_tokens: int,
) -> ClassificationResult:
    prompt = prompt_loader.get("classify")
    user_prompt = prompt["user_template"].format(subject=subject, sender=sender, body=body)
    output = llm_client.chat_completion(
        system_prompt=prompt["system"],
        user_prompt=user_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    ).content
    payload = _extract_json(output)
    urgency = int(payload.get("urgency_score", 0))
    urgency = max(0, min(100, urgency))
    confidence = float(payload.get("confidence", 0.5))
    confidence = max(0.0, min(1.0, confidence))
    return ClassificationResult(
        intent_label=str(payload.get("intent_label", "other")),
        intent_reason=str(payload.get("intent_reason", "")),
        urgency_score=urgency,
        urgency_reason=str(payload.get("urgency_reason", "")),
        confidence=confidence,
    )

