from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MAX_BYTES = 2_000_000
_BACKUPS = 3


class _JsonFormatter(logging.Formatter):
    """Minimal structured (JSON-per-line) log formatter, stdlib only."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(log_dir: Path) -> None:
    """Configure structured logging to logs/app.log and logs/error.log.

    - app.log captures INFO+ (full activity trail)
    - error.log captures ERROR+ only (quick triage)
    - console mirrors INFO+ for `docker compose up` visibility
    Idempotent: re-running (e.g. after a settings reload) won't duplicate handlers.

    Defensive: if ``log_dir`` cannot be used as a directory (e.g. a file exists
    in its place), logging falls back to console-only instead of crashing the
    whole application at startup.
    """
    formatter = _JsonFormatter()
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))

    handlers: list[logging.Handler] = [console]

    file_handlers_failed: Exception | None = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        app_handler = RotatingFileHandler(
            log_dir / "app.log", maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
        app_handler.setLevel(logging.INFO)
        app_handler.setFormatter(formatter)

        error_handler = RotatingFileHandler(
            log_dir / "error.log", maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        handlers = [app_handler, error_handler, console]
    except OSError as exc:
        # FileExistsError / NotADirectoryError / IsADirectoryError land here when
        # ``logs/`` has the wrong type. Keep running with console logging.
        file_handlers_failed = exc

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Drop our previously-installed handlers so reloads stay clean.
    for handler in list(root.handlers):
        if getattr(handler, "_email_app_handler", False):
            root.removeHandler(handler)
    for handler in handlers:
        handler._email_app_handler = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    if file_handlers_failed is not None:
        logging.getLogger(__name__).warning(
            "File logging disabled (%s); using console only. Check the logs/ folder.",
            file_handlers_failed,
        )
