from __future__ import annotations

import os
import stat
from pathlib import Path

from app.config.settings import AppSettings
from app.security.fs_validation import check_critical_paths, safe_mkdir

PLACEHOLDER_API_KEYS = {
    "",
    "your-openai-api-key",
    "changeme",
    "replace-me",
    "set-me",
}

MIN_REQUIRED_GMAIL_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
}


def _ensure_private_file_permissions(path: Path) -> str | None:
    if not path.exists() or os.name != "posix":
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        path.chmod(0o600)
        return f"Adjusted permissions to 600 for {path}"
    return None


def _ensure_private_dir_permissions(path: Path) -> str | None:
    if not path.exists() or os.name != "posix":
        return None
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        path.chmod(0o700)
        return f"Adjusted directory permissions to 700 for {path}"
    return None


def _sanitize_for_storage(value: str | None, *, enabled: bool, max_chars: int) -> str | None:
    if value is None:
        return None
    if not enabled:
        return None
    return value[: max(1, max_chars)].strip()


def sanitize_persisted_fields(
    *,
    snippet: str,
    summary: str | None,
    draft_text: str | None,
    settings: AppSettings,
) -> tuple[str, str | None, str | None]:
    snippet_out = _sanitize_for_storage(
        snippet,
        enabled=settings.database.persist_snippet,
        max_chars=settings.database.max_persisted_chars,
    )
    summary_out = _sanitize_for_storage(
        summary,
        enabled=settings.database.persist_ai_outputs,
        max_chars=settings.database.max_persisted_chars,
    )
    draft_out = _sanitize_for_storage(
        draft_text,
        enabled=settings.database.persist_ai_outputs,
        max_chars=settings.database.max_persisted_chars,
    )
    return snippet_out or "", summary_out, draft_out


def validate_and_prepare_runtime(settings: AppSettings) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    # Filesystem-shape preflight: a ``.env`` directory, a ``logs`` file, etc.
    # surface here as clear errors instead of crashing later on first use.
    for issue in check_critical_paths(settings):
        if issue.severity == "error":
            errors.append(issue.message)
        else:
            warnings.append(issue.message)

    api_key = settings.llm.api_key.strip()
    if api_key.lower() in PLACEHOLDER_API_KEYS:
        errors.append("LLM__API_KEY is missing or still set to a placeholder value.")

    if not settings.prompt_file.exists():
        errors.append(f"Prompt file not found: {settings.prompt_file}")

    if not settings.gmail.credentials_path.exists():
        errors.append(
            f"Gmail OAuth credentials file not found: {settings.gmail.credentials_path}. "
            "Create credentials/credentials.json from Google Cloud OAuth Desktop app."
        )

    missing_scopes = MIN_REQUIRED_GMAIL_SCOPES.difference(settings.gmail.scopes)
    if missing_scopes:
        errors.append(f"Gmail scopes missing required permissions: {sorted(missing_scopes)}")

    if "https://www.googleapis.com/auth/gmail.modify" in settings.gmail.scopes:
        warnings.append("gmail.modify scope is broader than needed for draft-only workflows.")

    for parent in (
        settings.gmail.credentials_path.parent,
        settings.gmail.token_path.parent,
        settings.database.sqlite_path.parent,
        settings.log_file.parent,
    ):
        # safe_mkdir never raises for a wrong-type path; it reports the problem.
        mkdir_issue = safe_mkdir(parent)
        if mkdir_issue is not None:
            errors.append(mkdir_issue.message)
            continue
        if str(parent) not in {"", "."}:
            dir_change = _ensure_private_dir_permissions(parent)
            if dir_change:
                warnings.append(dir_change)

    for path in (
        settings.gmail.credentials_path,
        settings.gmail.token_path,
        settings.database.sqlite_path,
        settings.log_file,
    ):
        change_note = _ensure_private_file_permissions(path)
        if change_note:
            warnings.append(change_note)

    return errors, warnings
