from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Allowed dropdown values (validated leniently; UI offers the canonical set).
LANGUAGES = {"auto", "fr", "en"}
TONES = {"professional", "friendly", "formal", "concise", "detailed"}
ORG_MATCH_TYPES = {"sender", "domain", "subject"}


class ReplyabilityWeights(BaseModel):
    """Score contributions per rule. Negative values lower replyability."""

    question_detected: int = 30
    known_contact: int = 20
    personal_sender: int = 20
    contains_newsletter: int = -50
    noreply_sender: int = -100


class OrgRule(BaseModel):
    """One Smart-Archive filing preference (Settings → AI Organization).

    "When an email matches ``value`` (by sender / domain / subject keyword),
    file it into ``label``." Consulted before the built-in rules and the AI.
    """

    match: str = "domain"  # sender | domain | subject
    value: str = ""
    label: str = ""


class UserConfig(BaseModel):
    """End-user-editable configuration, persisted as JSON in the data/ volume.

    Kept separate from AppSettings (secrets/provider) so it can hold multiline
    text and lists, and survive restarts via the mounted data/ directory.
    """

    # --- AI preferences ------------------------------------------------------
    custom_prompt: str = ""
    signature: str = ""
    default_language: str = "auto"  # auto | fr | en
    default_tone: str = "professional"  # professional | friendly | formal | concise | detailed

    # --- Filtering rules -----------------------------------------------------
    banned_senders: list[str] = Field(default_factory=list)
    allowed_senders: list[str] = Field(default_factory=list)
    ignore_keywords: list[str] = Field(default_factory=list)

    # --- Replyability engine -------------------------------------------------
    replyability_threshold: int = 50
    replyability_weights: ReplyabilityWeights = Field(default_factory=ReplyabilityWeights)

    # --- Smart Archive / AI Organization -------------------------------------
    organization_rules: list[OrgRule] = Field(default_factory=list)

    # --- Inbox UI ------------------------------------------------------------
    show_only_replyable: bool = True

    def normalized(self) -> "UserConfig":
        """Coerce dropdown values + clean list entries to safe defaults."""
        lang = (self.default_language or "auto").strip().lower()
        tone = (self.default_tone or "professional").strip().lower()
        self.default_language = lang if lang in LANGUAGES else "auto"
        self.default_tone = tone if tone in TONES else "professional"
        self.banned_senders = _clean_list(self.banned_senders)
        self.allowed_senders = _clean_list(self.allowed_senders)
        self.ignore_keywords = _clean_list(self.ignore_keywords)
        self.replyability_threshold = int(self.replyability_threshold)
        self.organization_rules = _clean_org_rules(self.organization_rules)
        return self


def _clean_org_rules(rules: list[OrgRule]) -> list[OrgRule]:
    out: list[OrgRule] = []
    for r in rules or []:
        if isinstance(r, dict):
            r = OrgRule(**r)
        match = (r.match or "domain").strip().lower()
        if match not in ORG_MATCH_TYPES:
            match = "domain"
        value = (r.value or "").strip().lower()  # matching is case-insensitive
        label = (r.label or "").strip()
        if value and label:
            out.append(OrgRule(match=match, value=value, label=label))
    return out


def _clean_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values or []:
        item = str(v).strip().lower()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class UserConfigStore:
    """Loads/saves UserConfig as JSON. Tolerant of a missing/corrupt file."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> UserConfig:
        if not self.path.exists():
            return UserConfig().normalized()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            return UserConfig(**data).normalized()
        except Exception as exc:  # noqa: BLE001 - never let bad config break startup
            logger.warning("Invalid user_config.json (%s); using defaults: %s", self.path, exc)
            return UserConfig().normalized()

    def save(self, config: UserConfig) -> UserConfig:
        config = config.normalized()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
        if os.name == "posix":
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        logger.info("Saved user config to %s", self.path)
        return config
