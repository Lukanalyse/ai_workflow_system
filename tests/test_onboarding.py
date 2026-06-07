"""Onboarding + fresh-install robustness tests.

Exercises the .env writer and settings loader against filesystem mistakes, and
simulates a completely fresh install (empty dir, nothing configured) to confirm
onboarding can proceed without a crash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.env_writer import update_env_file
from app.config.settings import AppSettings
from app.security.fs_validation import check_critical_paths, has_blocking
from app.security.startup_checks import validate_and_prepare_runtime


def test_update_env_rejects_directory(tmp_path):
    env = tmp_path / ".env"
    env.mkdir()
    with pytest.raises(RuntimeError) as exc:
        update_env_file(env, {"OPENAI_API_KEY": "sk-x"})
    assert "directory" in str(exc.value)
    assert ".env" in str(exc.value)


def test_update_env_creates_file_and_filters_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = tmp_path / ".env"
    written = update_env_file(env, {
        "OPENAI_API_KEY": "sk-live-1234",
        "NOT_ALLOWED": "nope",  # must be ignored by the allow-list
    })
    assert written == ["OPENAI_API_KEY"]
    text = env.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-live-1234" in text
    assert "NOT_ALLOWED" not in text


def test_update_env_upsert_is_idempotent(tmp_path):
    env = tmp_path / ".env"
    update_env_file(env, {"MONTHLY_BUDGET": "5"})
    update_env_file(env, {"MONTHLY_BUDGET": "10"})
    text = env.read_text(encoding="utf-8")
    assert text.count("MONTHLY_BUDGET=") == 1
    assert "MONTHLY_BUDGET=10" in text


def test_fresh_install_is_not_blocking(tmp_path, monkeypatch):
    """Empty project, nothing configured: must NOT block startup."""
    monkeypatch.chdir(tmp_path)
    issues = check_critical_paths(None)
    assert not has_blocking(issues)


def test_fresh_install_onboarding_writes_then_loads(tmp_path, monkeypatch):
    """Simulate the wizard's first action: write a key to a brand-new .env."""
    monkeypatch.chdir(tmp_path)
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(var, raising=False)

    env = Path(".env")
    update_env_file(env, {"OPENAI_API_KEY": "sk-fresh-5678", "LLM_PROVIDER": "openai"})
    assert env.is_file()

    # Filesystem stays clean after the write.
    assert not has_blocking(check_critical_paths(None))

    # Settings now resolve the freshly-written key (os.environ is updated too).
    settings = AppSettings()
    assert settings.llm.provider == "openai"
    assert settings.llm.api_key == "sk-fresh-5678"


def test_validate_and_prepare_runtime_reports_env_dir(tmp_path, monkeypatch):
    """The CLI/Streamlit preflight must report (not raise on) a .env directory."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").mkdir()
    settings = AppSettings()  # constructs even though .env is a dir
    errors, _warnings = validate_and_prepare_runtime(settings)
    assert any(".env" in e and "directory" in e for e in errors)


def test_validate_and_prepare_runtime_survives_logs_file(tmp_path, monkeypatch):
    """A 'logs' file where a folder is expected must not crash preflight."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").write_text("x", encoding="utf-8")
    settings = AppSettings()
    errors, _warnings = validate_and_prepare_runtime(settings)  # no exception
    assert any("logs" in e for e in errors)
