from __future__ import annotations

import os
import re
from pathlib import Path

# Keys the Settings page is allowed to write. Anything else is ignored so the
# web UI can never inject arbitrary config into .env.
ALLOWED_ENV_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
    "GOOGLE_CREDENTIALS_PATH",
    "MONTHLY_BUDGET",
    "ENABLE_EMAIL_SENDING",
}


def _upsert_line(env_text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", flags=re.MULTILINE)
    if pattern.search(env_text):
        return pattern.sub(line, env_text)
    suffix = "" if env_text.endswith("\n") or not env_text else "\n"
    return f"{env_text}{suffix}{line}\n"


def update_env_file(path: Path, updates: dict[str, str]) -> list[str]:
    """Write allowed key/value pairs into the .env file and process env.

    Also updates os.environ so the change takes effect on the next settings
    reload without a container restart (os.environ outranks the .env file in
    pydantic-settings). Returns the list of keys actually written.

    Raises a user-friendly ``RuntimeError`` (never a raw ``IsADirectoryError``)
    if ``.env`` exists with the wrong type.
    """
    if path.is_dir():
        raise RuntimeError(
            f"{path} exists but is a directory. "
            f"Remove it and create a file named {path.name}."
        )
    env_text = path.read_text(encoding="utf-8") if path.exists() else ""
    written: list[str] = []
    for raw_key, raw_value in updates.items():
        key = raw_key.strip().upper()
        if key not in ALLOWED_ENV_KEYS:
            continue
        value = (raw_value or "").strip()
        env_text = _upsert_line(env_text, key, value)
        os.environ[key] = value
        written.append(key)

    path.write_text(env_text, encoding="utf-8")
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return written
