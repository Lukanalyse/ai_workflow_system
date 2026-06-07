"""Filesystem-shape validation tests.

Covers the production-hardening requirement that the app never crashes on a
malformed installation. Each scenario builds the offending path inside an
isolated temp directory and asserts the validator reports an actionable issue
(and never raises).
"""

from __future__ import annotations

import os

import pytest

from app.security.fs_validation import (
    PathKind,
    PathSpec,
    check_critical_paths,
    has_blocking,
    safe_mkdir,
    validate_path,
)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """Run inside a clean temp 'project root' so relative defaults apply there."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _issue_for(name: str, issues):
    return next((i for i in issues if i.name == name), None)


# --- Required scenarios ------------------------------------------------------
def test_env_as_directory(project):
    (project / ".env").mkdir()
    issues = check_critical_paths(None)
    issue = _issue_for(".env", issues)
    assert issue is not None
    assert issue.code == "wrong_type_dir"
    assert issue.severity == "error"
    assert "directory" in issue.message
    assert "create a file named .env" in issue.message  # actionable remedy
    assert has_blocking(issues)


def test_credentials_as_directory(project):
    creds = project / "credentials" / "credentials.json"
    creds.mkdir(parents=True)
    issues = check_critical_paths(None)
    issue = _issue_for("credentials.json", issues)
    assert issue is not None
    assert issue.code == "wrong_type_dir"
    assert issue.severity == "error"


def test_storage_as_file(project):
    (project / "storage").write_text("oops", encoding="utf-8")
    issues = check_critical_paths(None)
    issue = _issue_for("storage/", issues)
    assert issue is not None
    assert issue.code == "wrong_type_file"
    assert issue.severity == "error"
    assert "file" in issue.message


def test_logs_as_file(project):
    (project / "logs").write_text("oops", encoding="utf-8")
    issues = check_critical_paths(None)
    issue = _issue_for("logs/", issues)
    assert issue is not None
    assert issue.code == "wrong_type_file"
    assert issue.severity == "error"


def test_data_as_file(project):
    (project / "data").write_text("oops", encoding="utf-8")
    issues = check_critical_paths(None)
    issue = _issue_for("data/", issues)
    assert issue is not None
    assert issue.code == "wrong_type_file"


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_unwritable_directory(project):
    storage = project / "storage"
    storage.mkdir()
    os.chmod(storage, 0o500)  # read+execute, no write
    try:
        if os.access(storage, os.W_OK):  # some filesystems ignore the bit
            pytest.skip("filesystem does not enforce the write bit")
        issues = check_critical_paths(None)
        issue = _issue_for("storage/", issues)
        assert issue is not None
        assert issue.code == "unwritable"
        assert issue.severity == "error"
    finally:
        os.chmod(storage, 0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_unreadable_file(project):
    env = project / ".env"
    env.write_text("OPENAI_API_KEY=x\n", encoding="utf-8")
    os.chmod(env, 0o000)
    try:
        if os.access(env, os.R_OK):
            pytest.skip("filesystem does not enforce the read bit")
        issues = check_critical_paths(None)
        issue = _issue_for(".env", issues)
        assert issue is not None
        assert issue.code in {"unreadable", "unwritable"}
        assert issue.severity == "error"
    finally:
        os.chmod(env, 0o600)


def test_missing_required_file_is_reported(project):
    spec = PathSpec(name="needed.json", path=project / "needed.json",
                    kind=PathKind.FILE, required=True)
    issue = validate_path(spec)
    assert issue is not None
    assert issue.code == "missing"
    # Missing-but-creatable is a warning, not a hard startup blocker.
    assert issue.severity == "warning"


def test_missing_optional_file_is_clean(project):
    # On a fresh install nothing exists yet — that must NOT be a blocking error.
    issues = check_critical_paths(None)
    assert not has_blocking(issues)


def test_file_where_directory_parent_expected(project):
    # credentials/ is a file, so credentials/credentials.json can't be created.
    (project / "credentials").write_text("x", encoding="utf-8")
    issues = check_critical_paths(None)
    issue = _issue_for("credentials.json", issues)
    assert issue is not None
    assert issue.code == "parent_not_dir"
    assert issue.severity == "error"


# --- safe_mkdir --------------------------------------------------------------
def test_safe_mkdir_creates(project):
    target = project / "a" / "b" / "c"
    assert safe_mkdir(target) is None
    assert target.is_dir()


def test_safe_mkdir_on_file_reports(project):
    (project / "blocker").write_text("x", encoding="utf-8")
    issue = safe_mkdir(project / "blocker")
    assert issue is not None
    assert issue.severity == "error"


def test_safe_mkdir_never_raises_on_file_parent(project):
    (project / "f").write_text("x", encoding="utf-8")
    issue = safe_mkdir(project / "f" / "child")  # parent is a file
    assert issue is not None
    assert issue.severity == "error"


def test_validate_path_never_raises():
    # Even on a clearly bogus path, validation returns (does not throw).
    spec = PathSpec(name="x", path=__import__("pathlib").Path("/proc/0/\0bad"),
                    kind=PathKind.FILE)
    # Should not raise regardless of the result.
    validate_path(spec)
