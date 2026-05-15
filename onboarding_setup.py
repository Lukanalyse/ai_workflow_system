from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
from getpass import getpass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
ENV_EXAMPLE_PATH = ROOT_DIR / ".env_EXAMPLE"
ENV_PATH = ROOT_DIR / ".env"
CREDS_DIR = ROOT_DIR / "credentials"
CREDS_PATH = CREDS_DIR / "credentials.json"
MIN_PYTHON = (3, 11)

REQUIRED_DIRS = [
    ROOT_DIR / "credentials",
    ROOT_DIR / "data",
    ROOT_DIR / "logs",
]

REQUIRED_IMPORTS = [
    ("dotenv", "python-dotenv"),
    ("pydantic", "pydantic"),
    ("pydantic_settings", "pydantic-settings"),
    ("httpx", "httpx"),
    ("yaml", "PyYAML"),
    ("googleapiclient", "google-api-python-client"),
    ("google_auth_oauthlib", "google-auth-oauthlib"),
    ("streamlit", "streamlit"),
]


def _hr() -> str:
    return "-" * 60


def _print_header() -> None:
    print(_hr())
    print("Welcome to Gmail AI Email Assistant Setup")
    print(_hr())
    print("This wizard will help you configure a safe local setup.")
    print("No emails are auto-sent. This project creates Gmail drafts only.")
    print()


def _step_banner(index: int, total: int, title: str) -> None:
    print(_hr())
    print(f"[{index}/{total}] {title}")
    print(_hr())


def _ok(message: str) -> None:
    print(f"[OK] {message}")


def _warn(message: str) -> None:
    print(f"[WARN] {message}")


def _error(message: str) -> None:
    print(f"[ERROR] {message}")


def _ask_yes_no(prompt: str, *, default: bool = False) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def _parse_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _upsert_env_text(env_text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", flags=re.MULTILINE)
    if pattern.search(env_text):
        return pattern.sub(line, env_text)
    suffix = "" if env_text.endswith("\n") else "\n"
    return f"{env_text}{suffix}{line}\n"


def _safe_chmod(path: Path, mode: int) -> None:
    if os.name != "posix" or not path.exists():
        return
    path.chmod(mode)


def _create_runtime_dirs() -> None:
    for directory in REQUIRED_DIRS:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            _ok(f"Created folder: {directory.relative_to(ROOT_DIR)}")
        else:
            _ok(f"Folder exists: {directory.relative_to(ROOT_DIR)}")
        _safe_chmod(directory, 0o700)


def _check_python() -> bool:
    current = sys.version_info[:3]
    if current >= MIN_PYTHON:
        _ok(f"Python version OK: {current[0]}.{current[1]}.{current[2]}")
        return True
    _error(
        f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required. "
        f"Current: {current[0]}.{current[1]}.{current[2]}"
    )
    return False


def _check_dependencies() -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for module_name, package_name in REQUIRED_IMPORTS:
        if importlib.util.find_spec(module_name) is None:
            missing.append((module_name, package_name))
    if not missing:
        _ok("All required Python dependencies are installed.")
        return missing
    for module_name, package_name in missing:
        _error(f"Missing dependency: module '{module_name}' (install package '{package_name}')")
    return missing


def _step_environment_check() -> dict[str, str]:
    _step_banner(1, 6, "Environment Check")
    env_values = _parse_env(ENV_PATH) if ENV_PATH.exists() else {}
    if not _check_python():
        print()
    missing = _check_dependencies()
    _create_runtime_dirs()

    if ENV_PATH.exists():
        _ok(".env file exists.")
    else:
        _warn("Missing .env file. It will be created in Step 4.")

    if CREDS_DIR.exists():
        _ok("credentials/ folder exists.")
    else:
        _warn("Missing credentials/ folder. It will be created automatically.")

    if missing:
        print()
        print("Install missing dependencies before first run:")
        print("  pip install -r requirements.txt")
    print()
    return env_values


def _print_gmail_oauth_instructions() -> None:
    print("Gmail OAuth setup instructions:")
    print("1. Open https://console.cloud.google.com")
    print("2. Create/select a project.")
    print("3. Enable Gmail API (APIs & Services -> Library).")
    print("4. Configure OAuth consent screen.")
    print("5. Create OAuth Client ID -> Desktop app.")
    print("6. Download credentials JSON.")
    print("7. Place it at: credentials/credentials.json")
    print()


def _step_gmail_oauth_setup() -> None:
    _step_banner(2, 6, "Gmail OAuth Setup")
    _print_gmail_oauth_instructions()
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    _safe_chmod(CREDS_DIR, 0o700)

    if CREDS_PATH.exists():
        _ok("Found credentials file at credentials/credentials.json")
        _safe_chmod(CREDS_PATH, 0o600)
        print()
        return

    _warn("credentials/credentials.json is missing.")
    src = input(
        "If you already downloaded it, paste the file path now (or press Enter to skip): "
    ).strip()
    if not src:
        _warn("Skipped file copy. You must place credentials/credentials.json before running the app.")
        print()
        return

    source_path = Path(src).expanduser()
    if not source_path.exists() or not source_path.is_file():
        _error(f"File not found: {source_path}")
        print()
        return

    shutil.copy2(source_path, CREDS_PATH)
    _safe_chmod(CREDS_PATH, 0o600)
    _ok("Copied Gmail credentials to credentials/credentials.json")
    print()


def _step_llm_setup(existing_env: dict[str, str]) -> dict[str, str]:
    _step_banner(3, 6, "LLM / GPT Setup")
    print("Use any OpenAI-compatible provider.")
    print("For OpenAI keys, create one at: https://platform.openai.com/api-keys")
    print()

    current_base_url = existing_env.get("LLM__BASE_URL", "https://api.openai.com/v1")
    current_model = existing_env.get("LLM__MODEL", "gpt-4.1-mini")
    current_key = existing_env.get("LLM__API_KEY", "")

    base_url = input(f"LLM base URL [{current_base_url}]: ").strip() or current_base_url
    model = input(f"LLM model [{current_model}]: ").strip() or current_model

    if current_key and current_key not in {"your-openai-api-key", "changeme", "replace-me", "set-me"}:
        keep = _ask_yes_no("An API key already exists in .env. Keep it?", default=True)
        if keep:
            api_key = current_key
        else:
            api_key = getpass("Paste new API key (input hidden): ").strip()
    else:
        api_key = getpass("Paste API key (input hidden): ").strip()

    if not api_key:
        _warn("API key not set. Validation will fail until LLM__API_KEY is configured.")

    print()
    return {
        "LLM__BASE_URL": base_url,
        "LLM__MODEL": model,
        "LLM__API_KEY": api_key,
    }


def _step_draft_default_setup(existing_env: dict[str, str]) -> dict[str, str]:
    _step_banner(4, 6, "Draft Mode Default")
    raw_current = existing_env.get("CREATE_DRAFTS_DEFAULT", "true").strip().lower()
    current_default = raw_current not in {"false", "0", "no"}
    print(
        "Choose default behavior for CLI runs.\n"
        "If enabled, drafts are created by default (still draft-only, never auto-send)."
    )
    enabled = _ask_yes_no(
        "Enable draft creation by default?",
        default=current_default,
    )
    print()
    return {"CREATE_DRAFTS_DEFAULT": "true" if enabled else "false"}


def _step_env_create_or_update(existing_env: dict[str, str], updates: dict[str, str]) -> None:
    _step_banner(5, 6, "Create or Update .env")
    if not ENV_PATH.exists():
        if not ENV_EXAMPLE_PATH.exists():
            _error(".env_EXAMPLE was not found. Cannot create .env automatically.")
            print()
            return
        shutil.copy2(ENV_EXAMPLE_PATH, ENV_PATH)
        _ok("Created .env from .env_EXAMPLE")
    else:
        _ok(".env already exists.")
        if not _ask_yes_no("Update existing .env with setup values?", default=False):
            _warn("Skipped .env update by user choice.")
            print()
            return

    env_text = ENV_PATH.read_text(encoding="utf-8")
    merged = dict(existing_env)
    merged.update(updates)
    for key, value in merged.items():
        if key in {"LLM__API_KEY", "LLM__BASE_URL", "LLM__MODEL", "CREATE_DRAFTS_DEFAULT"} and value:
            env_text = _upsert_env_text(env_text, key, value)

    ENV_PATH.write_text(env_text, encoding="utf-8")
    _safe_chmod(ENV_PATH, 0o600)
    _ok("Updated .env safely.")
    print()


def _step_validate_and_next_commands() -> int:
    _step_banner(6, 6, "Validation")
    cmd = [sys.executable, "-m", "app.gmail_cli", "--validate-config"]
    print("Running validation:")
    print(f"  {' '.join(cmd)}")
    print()

    result = subprocess.run(
        cmd,
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        _ok("Validation successful.")
    else:
        _error("Validation failed.")
        if result.stdout.strip():
            print("stdout:")
            print(result.stdout.strip())
        if result.stderr.strip():
            print("stderr:")
            print(result.stderr.strip())

    print()
    print(_hr())
    print("Next commands")
    print(_hr())
    print("Safe analyze-only mode:")
    print("  python -m app.gmail_cli --max-emails 5 --no-drafts")
    print()
    print("Create drafts (default-enabled or explicit):")
    print("  python -m app.gmail_cli --max-emails 5")
    print()
    print("Launch Streamlit:")
    print("  streamlit run app/ui/streamlit_app.py")
    print()
    return result.returncode


def main() -> int:
    _print_header()
    existing_env = _step_environment_check()
    _step_gmail_oauth_setup()
    llm_updates = _step_llm_setup(existing_env)
    draft_updates = _step_draft_default_setup(existing_env)
    all_updates = dict(llm_updates)
    all_updates.update(draft_updates)
    _step_env_create_or_update(existing_env, all_updates)
    return _step_validate_and_next_commands()


if __name__ == "__main__":
    raise SystemExit(main())
