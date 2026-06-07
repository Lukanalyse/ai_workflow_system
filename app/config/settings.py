from __future__ import annotations

from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()

# Per-provider defaults. The end user only sets an API key (and optionally a
# model); the rest is resolved here so no source edits are ever required.
OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"


class EmailFilterSettings(BaseModel):
    only_unread: bool = True
    after_date: datetime | None = None
    sender_filter: str = ""
    exclude_promotions: bool = True
    exclude_noreply: bool = True
    max_emails: int = 20


class LLMSettings(BaseModel):
    provider: str = "openai"  # "openai" | "anthropic"
    api_key: str = ""
    base_url: str = OPENAI_BASE_URL
    model: str = DEFAULT_OPENAI_MODEL
    temperature: float = 0.2
    max_tokens: int = 700
    default_tone: str = "formal"
    default_language: str = "en"
    max_input_chars: int = 12000


class DatabaseSettings(BaseModel):
    sqlite_path: Path = Path("data/email_workflow.db")
    persist_snippet: bool = False
    persist_ai_outputs: bool = False
    max_persisted_chars: int = 500


class GmailSettings(BaseModel):
    user_id: str = "me"
    credentials_path: Path = Path("credentials/credentials.json")
    # Legacy single-file token (still read & migrated automatically).
    token_path: Path = Path("token.json")
    # Multi-account-ready token storage: storage/tokens/<email>.json
    tokens_dir: Path = Path("storage/tokens")
    scopes: list[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.compose",
        ]
    )


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

    # --- Simple flat configuration (what the end user fills in .env) ----------
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    llm_provider: str = ""  # optional explicit override: "openai" | "anthropic"
    llm_model: str = ""  # optional model override
    llm_base_url: str = ""  # optional OpenAI-compatible endpoint override
    llm_temperature: str = ""  # optional 0.0–1.0 override (blank = provider default)
    google_credentials_path: str = ""
    # Where Google sends the user back after consent. Must match an authorized
    # redirect URI on the OAuth client (loopback is allowed for Desktop apps).
    oauth_redirect_uri: str = "http://localhost:3000/api/gmail/callback"

    # --- Structured config (legacy LLM__*/GMAIL__* still supported) -----------
    gmail: GmailSettings = Field(default_factory=GmailSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    filters: EmailFilterSettings = Field(default_factory=EmailFilterSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    prompt_file: Path = Path("app/config/prompts.yaml")
    pricing_file: Path = Path("app/config/pricing.yaml")
    user_config_path: Path = Path("data/user_config.json")
    log_dir: Path = Path("logs")
    log_file: Path = Path("logs/app.log")
    process_limit: int = 20
    create_drafts_default: bool = True
    monthly_budget: float = 5.0  # USD; surfaced on the usage dashboard
    enable_email_sending: bool = False  # send is OFF by default (security)

    @model_validator(mode="after")
    def _reconcile_flat_config(self) -> "AppSettings":
        """Fold the simple flat env vars into the structured LLM/Gmail config.

        Precedence for the active provider:
        1. explicit LLM_PROVIDER
        2. whichever single API key is present
        3. legacy LLM__* nested config
        """
        provider = (self.llm_provider or "").strip().lower()
        openai_key = self.openai_api_key.strip()
        anthropic_key = self.anthropic_api_key.strip()

        if provider not in {"openai", "anthropic"}:
            if anthropic_key and not openai_key:
                provider = "anthropic"
            elif openai_key:
                provider = "openai"
            else:
                provider = self.llm.provider or "openai"
        self.llm.provider = provider

        # Resolve an explicit model override (flat first, then legacy nested).
        explicit_model = self.llm_model.strip()
        if not explicit_model and self.llm.model and self.llm.model != DEFAULT_OPENAI_MODEL:
            explicit_model = self.llm.model

        if provider == "anthropic":
            self.llm.api_key = anthropic_key or self.llm.api_key
            self.llm.base_url = "https://api.anthropic.com"
            self.llm.model = explicit_model or DEFAULT_ANTHROPIC_MODEL
        else:
            self.llm.api_key = openai_key or self.llm.api_key
            self.llm.base_url = self.llm_base_url.strip() or self.llm.base_url or OPENAI_BASE_URL
            self.llm.model = explicit_model or DEFAULT_OPENAI_MODEL

        # Optional temperature override (clamped to a sane 0.0–1.0 range).
        temp_raw = self.llm_temperature.strip()
        if temp_raw:
            try:
                self.llm.temperature = max(0.0, min(1.0, float(temp_raw)))
            except ValueError:
                pass  # keep the default on a malformed value

        if self.google_credentials_path.strip():
            self.gmail.credentials_path = Path(self.google_credentials_path.strip())

        # Least privilege: only request the send scope when sending is enabled.
        # Changing this requires reconnecting Gmail from the Settings page (the
        # scope set changes, forcing fresh consent) — the app never sends
        # without it.
        send_scope = "https://www.googleapis.com/auth/gmail.send"
        if self.enable_email_sending and send_scope not in self.gmail.scopes:
            self.gmail.scopes = [*self.gmail.scopes, send_scope]

        # Keep log_file under log_dir so both structured logs land together.
        if self.log_file.parent != self.log_dir:
            self.log_file = self.log_dir / self.log_file.name
        return self


def get_settings() -> AppSettings:
    try:
        return AppSettings()
    except ValidationError as exc:
        lines = ["Invalid configuration in .env:"]
        for err in exc.errors():
            key = ".".join(str(part) for part in err.get("loc", []))
            lines.append(f"- {key}: {err.get('msg', 'invalid value')}")
        raise RuntimeError("\n".join(lines)) from exc
    except IsADirectoryError as exc:
        # pydantic-settings tries to read ``.env``; if it is a directory the
        # raw OSError would surface as a traceback. Translate it into a
        # user-friendly message (the filesystem validator reports the same).
        raise RuntimeError(
            ".env exists but is a directory. Remove it and create a file named .env."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not read configuration from .env: {exc}") from exc
