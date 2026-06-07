"""Reusable filesystem validation for the app's critical paths.

A non-technical user must never see a backend traceback because a file or
folder has the wrong type (e.g. a ``.env`` *directory* instead of a file).
This module inspects every critical path the application touches and turns
low-level ``OSError``/``IsADirectoryError``/``FileExistsError`` conditions into
structured, user-friendly issues that the API and onboarding UI can render.

Nothing in here ever raises for a filesystem condition — callers get a list of
:class:`PathIssue` objects back and decide how to surface them. The goal is
that startup stays operational whenever possible.

Detected conditions (requirement coverage):

* file expected but a directory was found      -> ``wrong_type_dir``
* directory expected but a file was found       -> ``wrong_type_file``
* a missing required resource                   -> ``missing``
* an unreadable resource                        -> ``unreadable``
* an unwritable resource                        -> ``unwritable``
* the parent path is occupied by a file         -> ``parent_not_dir``
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.config.settings import AppSettings


class PathKind(str, Enum):
    FILE = "file"
    DIR = "dir"


# Severity drives whether the application can keep running. ``error`` is
# blocking (services are not built); ``warning`` is informational (the app
# stays fully operational, the UI just nudges the user to fix it).
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class PathSpec:
    """Declares what we expect a critical path to be."""

    name: str  # human label, e.g. ".env" or "storage/"
    path: Path
    kind: PathKind
    # ``required`` only affects whether a *missing* resource is reported. A
    # type mismatch or permission problem is always reported regardless.
    required: bool = False
    need_read: bool = True
    need_write: bool = False
    remedy: str = ""  # extra "how to fix" guidance appended to the message


@dataclass(frozen=True)
class PathIssue:
    """A single problem found with a critical path."""

    name: str
    path: str
    kind: str  # "file" | "dir"
    code: str  # wrong_type_dir | wrong_type_file | missing | unreadable | ...
    severity: str  # error | warning
    message: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


def _issue(spec: PathSpec, code: str, severity: str, message: str) -> PathIssue:
    full = message
    if severity == SEVERITY_ERROR and spec.remedy:
        full = f"{message} {spec.remedy}"
    return PathIssue(
        name=spec.name,
        path=str(spec.path),
        kind=spec.kind.value,
        code=code,
        severity=severity,
        message=full,
    )


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _readable(path: Path) -> bool:
    return os.access(path, os.R_OK)


def _writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


def _validate_file(spec: PathSpec) -> PathIssue | None:
    path = spec.path
    try:
        exists = path.exists() or path.is_symlink()
    except OSError as exc:  # pragma: no cover - extremely unusual
        return _issue(spec, "unreadable", SEVERITY_ERROR,
                      f"{spec.name} could not be inspected ({exc}).")

    if exists and _is_dir(path):
        return _issue(
            spec, "wrong_type_dir", SEVERITY_ERROR,
            f"{spec.name} exists but is a directory.",
        )

    if exists:
        if spec.need_read and not _readable(path):
            return _issue(spec, "unreadable", SEVERITY_ERROR,
                          f"{spec.name} exists but is not readable. Fix its permissions.")
        if spec.need_write and not _writable(path):
            return _issue(spec, "unwritable", SEVERITY_ERROR,
                          f"{spec.name} exists but is not writable. Fix its permissions.")
        return None

    # Missing file: the parent must be a real directory (or absent, since it
    # can be created). If the parent is occupied by a file, creation fails.
    parent = path.parent
    if parent != path and _is_file(parent):
        return _issue(spec, "parent_not_dir", SEVERITY_ERROR,
                      f"{spec.name} cannot be created because {parent} is a file, not a folder.")
    if spec.need_write and parent.exists() and not _writable(parent):
        return _issue(spec, "unwritable", SEVERITY_ERROR,
                      f"{spec.name} cannot be created because its folder {parent} is not writable.")
    if spec.required:
        return _issue(spec, "missing", SEVERITY_WARNING,
                      f"{spec.name} is missing.")
    return None


def _validate_dir(spec: PathSpec) -> PathIssue | None:
    path = spec.path
    try:
        exists = path.exists() or path.is_symlink()
    except OSError as exc:  # pragma: no cover
        return _issue(spec, "unreadable", SEVERITY_ERROR,
                      f"{spec.name} could not be inspected ({exc}).")

    if exists and _is_file(path):
        return _issue(
            spec, "wrong_type_file", SEVERITY_ERROR,
            f"{spec.name} exists but is a file, not a folder.",
        )

    if exists and _is_dir(path):
        if spec.need_read and not _readable(path):
            return _issue(spec, "unreadable", SEVERITY_ERROR,
                          f"{spec.name} exists but is not readable. Fix its permissions.")
        if spec.need_write and not _writable(path):
            return _issue(spec, "unwritable", SEVERITY_ERROR,
                          f"{spec.name} exists but is not writable. Fix its permissions.")
        return None

    # Missing directory: it will be auto-created, so only the parent matters.
    parent = path.parent
    if parent != path and _is_file(parent):
        return _issue(spec, "parent_not_dir", SEVERITY_ERROR,
                      f"{spec.name} cannot be created because {parent} is a file, not a folder.")
    if spec.required:
        return _issue(spec, "missing", SEVERITY_WARNING, f"{spec.name} is missing.")
    return None


def validate_path(spec: PathSpec) -> PathIssue | None:
    """Validate a single path spec. Never raises."""
    if spec.kind is PathKind.DIR:
        return _validate_dir(spec)
    return _validate_file(spec)


def validate_paths(specs: list[PathSpec]) -> list[PathIssue]:
    """Validate many specs, returning every issue found (errors first)."""
    issues = [issue for spec in specs if (issue := validate_path(spec)) is not None]
    issues.sort(key=lambda i: 0 if i.severity == SEVERITY_ERROR else 1)
    return issues


# --- Critical-path specs -----------------------------------------------------
# Defaults mirror the application's settings so validation works even when the
# settings cannot be loaded (e.g. because ``.env`` itself is a directory).
_DEFAULTS: dict[str, Path] = {
    "env": Path(".env"),
    "credentials": Path("credentials/credentials.json"),
    "token": Path("token.json"),
    "database": Path("data/email_workflow.db"),
    "storage": Path("storage"),
    "logs": Path("logs"),
    "data": Path("data"),
}


def build_specs(settings: "AppSettings | None" = None) -> list[PathSpec]:
    """Build the critical-path specs, preferring live settings when available."""
    if settings is not None:
        env_path = Path(".env")
        credentials = settings.gmail.credentials_path
        token = settings.gmail.token_path
        database = settings.database.sqlite_path
        # ``tokens_dir`` is ``storage/tokens``; the user-facing root is its parent.
        storage = settings.gmail.tokens_dir.parent
        logs = settings.log_dir
        data = settings.user_config_path.parent
    else:
        env_path = _DEFAULTS["env"]
        credentials = _DEFAULTS["credentials"]
        token = _DEFAULTS["token"]
        database = _DEFAULTS["database"]
        storage = _DEFAULTS["storage"]
        logs = _DEFAULTS["logs"]
        data = _DEFAULTS["data"]

    return [
        PathSpec(
            name=".env", path=env_path, kind=PathKind.FILE, need_write=True,
            remedy="Remove it and create a file named .env (or let onboarding create one).",
        ),
        PathSpec(
            name="credentials.json", path=credentials, kind=PathKind.FILE,
            remedy="Remove it and upload your Google OAuth client file from Settings → Gmail.",
        ),
        PathSpec(
            name="token.json", path=token, kind=PathKind.FILE,
            remedy="Remove it; a fresh token is created when you connect Gmail.",
        ),
        PathSpec(
            name="the SQLite database", path=database, kind=PathKind.FILE, need_write=True,
            remedy="Remove the folder so the database file can be created.",
        ),
        PathSpec(
            name="storage/", path=storage, kind=PathKind.DIR, need_write=True,
            remedy="Remove the file so the storage folder can be created.",
        ),
        PathSpec(
            name="logs/", path=logs, kind=PathKind.DIR, need_write=True,
            remedy="Remove the file so the logs folder can be created.",
        ),
        PathSpec(
            name="data/", path=data, kind=PathKind.DIR, need_write=True,
            remedy="Remove the file so the data folder can be created.",
        ),
    ]


def check_critical_paths(settings: "AppSettings | None" = None) -> list[PathIssue]:
    """Validate every critical path. Never raises."""
    return validate_paths(build_specs(settings))


def has_blocking(issues: list[PathIssue]) -> bool:
    """True if any issue is severe enough to prevent safe startup."""
    return any(i.severity == SEVERITY_ERROR for i in issues)


def first_blocking_message(issues: list[PathIssue]) -> str | None:
    for i in issues:
        if i.severity == SEVERITY_ERROR:
            return i.message
    return None


def issues_to_dicts(issues: list[PathIssue]) -> list[dict]:
    return [i.to_dict() for i in issues]


def safe_mkdir(path: Path) -> PathIssue | None:
    """Create ``path`` (and parents) defensively.

    Returns ``None`` on success or an already-correct directory, and a
    :class:`PathIssue` describing the problem if creation is impossible
    because the path (or a parent) is the wrong type. Never raises for a
    filesystem-shape problem.
    """
    spec = PathSpec(name=str(path), path=path, kind=PathKind.DIR, need_write=True)
    existing = validate_path(spec)
    if existing is not None and existing.severity == SEVERITY_ERROR:
        return existing
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError, PermissionError, OSError):
        return validate_path(spec) or _issue(
            spec, "unwritable", SEVERITY_ERROR,
            f"{path} could not be created.",
        )
    return None
