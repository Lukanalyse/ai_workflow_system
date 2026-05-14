from __future__ import annotations

from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class EmailFilterSettings(BaseModel):
    only_unread: bool = True
    after_date: datetime | None = None
    sender_filter: str = ""
    exclude_promotions: bool = True
    exclude_noreply: bool = True
    max_emails: int = 20


class LLMSettings(BaseModel):
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1-mini"
    temperature: float = 0.2
    max_tokens: int = 700
    default_tone: str = "formal"
    default_language: str = "en"


class DatabaseSettings(BaseModel):
    sqlite_path: Path = Path("data/email_workflow.db")


class GmailSettings(BaseModel):
    user_id: str = "me"
    credentials_path: Path = Path("credentials/credentials.json")
    token_path: Path = Path("token.json")
    scopes: list[str] = Field(
        default_factory=lambda: [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/gmail.compose",
        ]
    )


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

    gmail: GmailSettings = Field(default_factory=GmailSettings)
    llm: LLMSettings
    filters: EmailFilterSettings = Field(default_factory=EmailFilterSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    prompt_file: Path = Path("app/config/prompts.yaml")
    log_file: Path = Path("logs/app.log")
    process_limit: int = 20


def get_settings() -> AppSettings:
    return AppSettings()
