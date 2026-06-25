from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config.env_writer import update_env_file
from app.config.settings import AppSettings, get_settings
from app.config.user_config import UserConfig, UserConfigStore
from app.database.sqlite_manager import SQLiteManager
from app.email.replyability import ReplyabilityScorer
from app.logging_config import configure_logging
from app.providers.gmail.provider import GmailProvider
from app.security.fs_validation import (
    check_critical_paths,
    first_blocking_message,
    has_blocking,
    issues_to_dicts,
    safe_mkdir,
)
from app.services.bulk_service import BULK_MAX, BulkService
from app.services.cost_service import CostService
from app.services.draft_service import DraftService
from app.services.email_service import EmailService
from app.services.llm_service import LLMService

# Per-email fallback token averages used for cost estimates before any history
# exists (one summarize + one draft call). Refined automatically from real
# usage once the DB has data (see usage_averages()).
DEFAULT_AVG_INPUT_PER_EMAIL = 1800.0
DEFAULT_AVG_OUTPUT_PER_EMAIL = 450.0

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
ENV_PATH = Path(".env")


class ServiceContainer:
    """Builds and holds the service graph; rebuildable on settings change.

    Startup is resilient: a filesystem problem on a critical path (e.g. ``.env``
    being a directory) puts the container into *degraded* mode instead of
    crashing the process. In degraded mode the service graph is not built, but
    the app still serves ``/api/health`` and ``/api/setup/status`` so the
    onboarding UI can tell the user exactly what to fix.
    """

    def __init__(self) -> None:
        self.degraded: bool = True
        self.fs_issues: list = []
        self.init_error: str | None = None
        self.settings: AppSettings | None = None
        self.sqlite: SQLiteManager | None = None
        self.provider: GmailProvider | None = None
        self.cost_service: CostService | None = None
        self.user_config_store: UserConfigStore | None = None
        self.user_config: UserConfig | None = None
        self.scorer: ReplyabilityScorer | None = None
        self.llm_service: LLMService | None = None
        self.email_service: EmailService | None = None
        self.draft_service: DraftService | None = None
        self.bulk_service: BulkService | None = None
        self.reload()

    def _teardown(self) -> None:
        for attr in (
            "settings", "sqlite", "provider", "cost_service", "user_config_store",
            "user_config", "scorer", "llm_service", "email_service",
            "draft_service", "bulk_service",
        ):
            setattr(self, attr, None)

    def reload(self) -> None:
        # 1) Filesystem preflight — runs even if settings cannot be loaded
        #    (e.g. ``.env`` is a directory), so we always have something to show.
        try:
            preflight_settings = get_settings()
        except Exception:  # noqa: BLE001 - settings may be unreadable; use defaults
            preflight_settings = None
        self.fs_issues = check_critical_paths(preflight_settings)

        if has_blocking(self.fs_issues):
            self.degraded = True
            self.init_error = first_blocking_message(self.fs_issues)
            self._teardown()
            logger.error("Startup degraded — filesystem issue: %s", self.init_error)
            return

        # 2) Build the service graph. Any failure degrades rather than crashes.
        try:
            settings = get_settings()
            configure_logging(settings.log_dir)
            self.settings = settings
            self.sqlite = SQLiteManager(settings.database.sqlite_path)
            self.provider = GmailProvider(settings)
            self.cost_service = CostService(settings.pricing_file)
            self.user_config_store = UserConfigStore(settings.user_config_path)
            self.user_config = self.user_config_store.load()
            self.scorer = ReplyabilityScorer(self.user_config)
            self.llm_service = LLMService(
                settings,
                sqlite=self.sqlite,
                cost_service=self.cost_service,
                user_config=self.user_config,
            )
            self.email_service = EmailService(self.provider, self.sqlite, self.scorer)
            self.draft_service = DraftService(
                llm_service=self.llm_service,
                provider=self.provider,
                sqlite=self.sqlite,
                settings=settings,
            )
            self.bulk_service = BulkService(
                email_service=self.email_service,
                draft_service=self.draft_service,
            )
            # Re-validate using the live settings paths (custom locations).
            self.fs_issues = check_critical_paths(settings)
            self.degraded = has_blocking(self.fs_issues)
            self.init_error = first_blocking_message(self.fs_issues)
        except Exception as exc:  # noqa: BLE001 - never crash the web process
            self.degraded = True
            self.init_error = str(exc)
            self._teardown()
            logger.exception("Service initialization failed; running in degraded mode")
            return

        logger.info("Services initialized (llm_provider=%s)", settings.llm.provider)

    def fs_status(self) -> str:
        if has_blocking(self.fs_issues):
            return "error"
        return "warning" if self.fs_issues else "ok"


container = ServiceContainer()
app = FastAPI(title="AI Email Assistant", docs_url=None, redoc_url=None)

# Endpoints that read or mutate live state need a fully-built service graph. In
# degraded mode (e.g. ``.env`` is a directory, ``logs/`` is a file) the graph is
# not built, so these endpoints would otherwise dereference ``None`` and crash
# with a traceback. ``_require_ready`` converts that into a clean, actionable
# 503 carrying the same message the health/setup endpoints expose.
NOT_READY_FALLBACK = (
    "The application is not ready because of a filesystem problem. "
    "Open the app to see what needs fixing, or check /api/health."
)


def _require_ready() -> None:
    if container.degraded or container.settings is None:
        raise HTTPException(
            status_code=503,
            detail=container.init_error or NOT_READY_FALLBACK,
        )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last line of defence: turn any unhandled error into a clean JSON 500.

    No traceback and no exception text (which could contain a path or secret)
    reaches the client — the full detail is logged server-side for triage.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please check the application logs."},
    )


# --- Request bodies ----------------------------------------------------------
class DraftRequest(BaseModel):
    tone: str | None = None
    language: str | None = None


class SaveDraftRequest(BaseModel):
    draft: str


class SendEmailRequest(BaseModel):
    body: str
    confirm: bool = False


class BulkPreviewRequest(BaseModel):
    count: int = 20
    mode: str = "generate"


class SettingsRequest(BaseModel):
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_temperature: float | None = None
    google_credentials_path: str | None = None
    monthly_budget: float | None = None
    enable_email_sending: bool | None = None


class GmailCredentialsRequest(BaseModel):
    credentials: str  # raw contents of the Google OAuth client JSON file


def _mask(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    return f"…{value[-4:]}" if len(value) > 4 else "set"


def _available_models() -> dict[str, list[str]]:
    """Model dropdown options per provider, sourced from the pricing table."""
    from app.config.settings import DEFAULT_ANTHROPIC_MODEL, DEFAULT_OPENAI_MODEL

    fallback = {
        "openai": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o"],
        "anthropic": ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"],
    }
    try:
        import yaml

        data = yaml.safe_load(container.settings.pricing_file.read_text(encoding="utf-8")) or {}
        providers = data.get("providers", {})
        models = {p: sorted(m.keys()) for p, m in providers.items() if isinstance(m, dict)}
        for key in ("openai", "anthropic"):
            if not models.get(key):
                models[key] = fallback[key]
        return models
    except Exception:  # noqa: BLE001 - dropdown must never break settings
        return fallback


# --- API ---------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    fs_section = {
        "status": container.fs_status(),
        "issues": issues_to_dicts(container.fs_issues),
    }

    # Degraded: the service graph was never built. Report the filesystem problem
    # instead of dereferencing ``None`` services (which would 500 with a trace).
    if container.degraded or container.settings is None:
        unavailable = "Unavailable until the filesystem problem above is fixed."
        return {
            "status": "error",
            "filesystem": fs_section,
            "gmail": {"status": "unknown", "detail": unavailable},
            "llm": {"status": "unknown", "detail": unavailable},
            "database": {"status": "unknown", "detail": unavailable},
        }

    gmail_status, gmail_detail = container.provider.health()
    llm_status, llm_detail = container.llm_service.health()
    try:
        container.sqlite.list_recent_gmail(limit=1)
        db_status, db_detail = "ok", "Database reachable."
    except Exception as exc:  # noqa: BLE001
        db_status, db_detail = "error", f"Database error: {exc}"

    overall = "ok"
    if "error" in {gmail_status, db_status} or llm_status == "not_configured":
        overall = "degraded"
    if gmail_status == "not_authenticated":
        overall = "degraded"
    # A non-blocking filesystem warning (e.g. a missing-but-creatable resource)
    # should nudge the status to "degraded" without taking the app down.
    if fs_section["status"] != "ok" and overall == "ok":
        overall = "degraded"
    return {
        "status": overall,
        "filesystem": fs_section,
        "gmail": {"status": gmail_status, "detail": gmail_detail},
        "llm": {"status": llm_status, "detail": llm_detail},
        "database": {"status": db_status, "detail": db_detail},
    }


@app.get("/api/emails")
def list_emails(max: str = "20", status: str = "unread") -> dict:
    _require_ready()
    # ``max`` is a string so the UI can request "all"; numeric values stay
    # backward-compatible. "all" maps to the listing safety ceiling.
    from app.email.gmail_reader import LIST_MAX_RESULTS

    raw = (max or "20").strip().lower()
    if raw in {"all", "tous", "0"}:
        max_emails = LIST_MAX_RESULTS
    else:
        try:
            max_emails = int(raw)
        except ValueError:
            max_emails = 20
    try:
        candidates = container.email_service.list_candidates(
            max_emails=max_emails, status=status
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to list emails")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"emails": [asdict(c) for c in candidates]}


@app.post("/api/emails/{message_id}/draft")
def generate_draft(message_id: str, body: DraftRequest) -> dict:
    _require_ready()
    try:
        email = container.email_service.get_message(message_id)
        result = container.draft_service.generate(email, tone=body.tone, language=body.language)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to generate draft for %s", message_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"summary": result.summary, "draft": result.draft}


@app.post("/api/emails/{message_id}/save-draft")
def save_draft(message_id: str, body: SaveDraftRequest) -> dict:
    _require_ready()
    if not body.draft.strip():
        raise HTTPException(status_code=400, detail="Draft text is empty.")
    try:
        email = container.email_service.get_message(message_id)
        draft_id = container.draft_service.save(email, draft_text=body.draft)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save draft for %s", message_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"draft_id": draft_id}


@app.post("/api/emails/{message_id}/send")
def send_email(message_id: str, body: SendEmailRequest) -> dict:
    _require_ready()
    # Layer 1 of the send gate: config flag (also enforced in the provider).
    if not container.settings.enable_email_sending:
        raise HTTPException(status_code=403, detail="Email sending is disabled.")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Sending requires explicit confirmation.")
    if not body.body.strip():
        raise HTTPException(status_code=400, detail="Email body is empty.")
    try:
        email = container.email_service.get_message(message_id)
        result = container.draft_service.send(email, body=body.body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send email for %s", message_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"message_id": result.message_id, "thread_id": result.thread_id}


# --- Bulk --------------------------------------------------------------------
def _avg_tokens_per_email() -> tuple[float, float]:
    try:
        avg_in, avg_out = container.sqlite.usage_averages()
    except Exception:  # noqa: BLE001
        avg_in, avg_out = 0.0, 0.0
    if avg_in <= 0 or avg_out <= 0:
        return DEFAULT_AVG_INPUT_PER_EMAIL, DEFAULT_AVG_OUTPUT_PER_EMAIL
    return avg_in, avg_out


def _month_start_iso() -> str:
    now = datetime.now(tz=timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


@app.post("/api/bulk/preview")
def bulk_preview(body: BulkPreviewRequest) -> dict:
    _require_ready()
    count = max(1, min(int(body.count), BULK_MAX))
    avg_in, avg_out = _avg_tokens_per_email()
    s = container.settings
    estimate = container.cost_service.estimate_bulk(
        count=count,
        provider=s.llm.provider,
        model=s.llm.model,
        avg_input_per_email=avg_in,
        avg_output_per_email=avg_out,
    )
    month_cost = container.sqlite.usage_cost_since(since_iso=_month_start_iso())
    budget = float(s.monthly_budget)
    projected = month_cost + estimate["est_cost"]
    exceeds = budget > 0 and projected > budget
    estimate.update(
        {
            "month_cost": round(month_cost, 4),
            "monthly_budget": round(budget, 2),
            "projected_month_cost": round(projected, 4),
            "exceeds_budget": exceeds,
        }
    )
    return estimate


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.get("/api/bulk/stream")
def bulk_stream(
    request: Request,
    count: int = 20,
    mode: str = "generate",
    tone: str | None = None,
    language: str | None = None,
) -> StreamingResponse:
    _require_ready()

    def event_generator():
        try:
            for event in container.bulk_service.run(
                count=count, mode=mode, tone=tone, language=language
            ):
                yield _sse(event)
        except Exception as exc:  # noqa: BLE001 - never break the stream silently
            logger.exception("Bulk stream failed")
            yield _sse({"type": "error", "error": str(exc)})

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=headers
    )


@app.get("/api/usage/summary")
def usage_summary() -> dict:
    _require_ready()
    now = datetime.now(tz=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        today = container.sqlite.usage_summary(since_iso=today_start)
        month = container.sqlite.usage_summary(since_iso=month_start)
        month_cost = month.cost
        breakdown = container.sqlite.usage_breakdown(since_iso=month_start)
        daily = container.sqlite.daily_costs(since_iso=month_start)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build usage summary")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    budget = float(container.settings.monthly_budget)
    remaining = max(0.0, budget - month_cost) if budget > 0 else 0.0
    pct = round((month_cost / budget) * 100) if budget > 0 else 0
    return {
        "currency": container.cost_service.currency,
        "today": {
            "emails_analyzed": today.emails_analyzed,
            "drafts_generated": today.drafts_generated,
            "drafts_sent": today.drafts_sent,
            "input_tokens": today.input_tokens,
            "output_tokens": today.output_tokens,
            "total_tokens": today.total_tokens,
            "cost": round(today.cost, 4),
        },
        "month": {
            "cost": round(month_cost, 4),
            "budget": round(budget, 2),
            "remaining": round(remaining, 4),
            "pct": pct,
            "emails_analyzed": month.emails_analyzed,
            "drafts_generated": month.drafts_generated,
            "drafts_sent": month.drafts_sent,
            "total_tokens": month.total_tokens,
        },
        "daily": daily,
        "providers": breakdown["providers"],
        "models": breakdown["models"],
    }


@app.get("/api/settings")
def read_settings() -> dict:
    _require_ready()
    s = container.settings
    # Show the *effective* key for the active provider so a key supplied via
    # legacy LLM__API_KEY still reads as configured, not blank.
    openai_key = s.llm.api_key if s.llm.provider == "openai" else s.openai_api_key
    anthropic_key = s.llm.api_key if s.llm.provider == "anthropic" else s.anthropic_api_key
    return {
        "llm_provider": s.llm.provider,
        "llm_model": s.llm.model,
        "llm_temperature": round(float(s.llm.temperature), 2),
        "openai_api_key": _mask(openai_key),
        "anthropic_api_key": _mask(anthropic_key),
        "google_credentials_path": str(s.gmail.credentials_path),
        "monthly_budget": round(float(s.monthly_budget), 2),
        "email_sending_enabled": bool(s.enable_email_sending),
        "available_models": _available_models(),
    }


@app.post("/api/settings")
def write_settings(body: SettingsRequest) -> dict:
    # Only forward keys the user actually provided (non-None). Masked values
    # echoed back from the UI start with "…" and must be ignored.
    updates: dict[str, str] = {}
    if body.openai_api_key is not None and not body.openai_api_key.startswith("…"):
        updates["OPENAI_API_KEY"] = body.openai_api_key
    if body.anthropic_api_key is not None and not body.anthropic_api_key.startswith("…"):
        updates["ANTHROPIC_API_KEY"] = body.anthropic_api_key
    if body.llm_provider is not None:
        updates["LLM_PROVIDER"] = body.llm_provider
    if body.llm_model is not None:
        updates["LLM_MODEL"] = body.llm_model
    if body.llm_temperature is not None:
        updates["LLM_TEMPERATURE"] = str(max(0.0, min(1.0, float(body.llm_temperature))))
    if body.google_credentials_path is not None:
        updates["GOOGLE_CREDENTIALS_PATH"] = body.google_credentials_path
    if body.monthly_budget is not None:
        updates["MONTHLY_BUDGET"] = str(max(0.0, float(body.monthly_budget)))
    if body.enable_email_sending is not None:
        updates["ENABLE_EMAIL_SENDING"] = "true" if body.enable_email_sending else "false"

    try:
        written = update_env_file(ENV_PATH, updates)
    except RuntimeError as exc:
        # ``.env`` is a directory (or otherwise the wrong type) — actionable.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("Failed to write .env")
        raise HTTPException(
            status_code=400,
            detail=f"Could not write .env: {exc}",
        ) from exc
    container.reload()
    # Log only the *names* of changed keys, never their secret values.
    logger.info("Settings updated via web UI: %s", written)
    if container.degraded or container.settings is None:
        # The keys were saved, but another filesystem problem still blocks
        # startup. Report it instead of crashing on read_settings().
        return {
            "updated": written,
            "degraded": True,
            "detail": container.init_error or NOT_READY_FALLBACK,
        }
    return {"updated": written, **read_settings()}


# --- User config (AI prefs, filtering rules, replyability engine) ------------
@app.get("/api/config")
def read_config() -> dict:
    _require_ready()
    return container.user_config.model_dump()


@app.post("/api/config")
def write_config(body: UserConfig) -> dict:
    _require_ready()
    saved = container.user_config_store.save(body)
    container.reload()
    logger.info("User config updated via web UI")
    return saved.model_dump()


# --- Gmail connection (OAuth from the UI) ------------------------------------
@app.get("/api/gmail/status")
def gmail_status() -> dict:
    _require_ready()
    try:
        return container.provider.connection_status()
    except Exception as exc:  # noqa: BLE001 - status must never 500 the UI
        logger.exception("Failed to read Gmail status")
        return {
            "connected": False,
            "email": None,
            "valid": False,
            "expired": False,
            "last_refresh": None,
            "scopes": [],
            "credentials_available": False,
            "accounts": [],
            "send_scope": False,
            "error": str(exc),
        }


@app.get("/api/gmail/connect")
def gmail_connect() -> dict:
    """Return the Google consent URL for the UI to open in a new window."""
    _require_ready()
    logger.info("Gmail OAuth: /connect requested")
    try:
        url = container.provider.begin_oauth()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to start Gmail OAuth")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"auth_url": url}


@app.get("/api/gmail/callback")
def gmail_callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Google redirects here after consent. Store the token, then bounce back."""
    # Log only presence flags — never the code, state, or token.
    logger.info(
        "Gmail OAuth: /callback hit (has_code=%s, has_state=%s, error=%s)",
        bool(code), bool(state), error or "none",
    )
    if container.degraded or container.provider is None:
        return RedirectResponse(url="/?gmail=error&detail=not_ready")
    if error:
        return RedirectResponse(url=f"/?gmail=error&detail={error}")
    if not code or not state:
        return RedirectResponse(url="/?gmail=error&detail=missing_code")
    try:
        email = container.provider.complete_oauth(code=code, state=state)
    except Exception as exc:  # noqa: BLE001 - report failure to the UI
        logger.exception("Gmail OAuth callback failed")
        return RedirectResponse(url=f"/?gmail=error&detail={exc}")
    logger.info("Gmail connected via web OAuth: %s", email)
    return RedirectResponse(url=f"/?gmail=connected&email={email}")


@app.post("/api/gmail/disconnect")
def gmail_disconnect() -> dict:
    _require_ready()
    try:
        container.provider.disconnect()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to disconnect Gmail")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    logger.info("Gmail disconnected via web UI")
    return {"disconnected": True}


@app.post("/api/gmail/credentials")
def gmail_credentials(body: GmailCredentialsRequest) -> dict:
    """Store an uploaded Google OAuth client file (credentials.json).

    Lets a non-technical user drop in the file from Settings → Gmail instead of
    placing it on disk by hand. Validated as a desktop/web OAuth client.
    """
    _require_ready()
    raw = (body.credentials or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty credentials file.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not ({"installed", "web"} & parsed.keys()):
        raise HTTPException(
            status_code=400,
            detail="This does not look like a Google OAuth client file "
            "(missing the 'installed' or 'web' section).",
        )
    path = container.settings.gmail.credentials_path
    # Defensive write: never let a wrong-type folder/file surface as a traceback.
    mkdir_issue = safe_mkdir(path.parent)
    if mkdir_issue is not None:
        raise HTTPException(status_code=400, detail=mkdir_issue.message)
    if path.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"{path} exists but is a directory. Remove it so the "
            "credentials file can be saved.",
        )
    try:
        path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.exception("Failed to store uploaded credentials")
        raise HTTPException(
            status_code=400, detail=f"Could not save the credentials file: {exc}"
        ) from exc
    if os.name == "posix":
        try:
            path.chmod(0o600)
        except OSError:
            pass
    container.reload()
    logger.info("Stored uploaded Google OAuth client at %s", path)
    return {"stored": True, "path": str(path)}


# --- First-run setup status --------------------------------------------------
@app.get("/api/setup/status")
def setup_status() -> dict:
    """Drive the first-run wizard: which steps still need attention.

    Always safe to call — even in degraded mode the wizard relies on this to
    explain what the user must fix, so it never dereferences ``None`` services.
    """
    fs_issues = issues_to_dicts(container.fs_issues)
    filesystem_ok = not has_blocking(container.fs_issues)

    if container.degraded or container.settings is None or container.provider is None:
        return {
            "llm_ready": False,
            "llm_provider": None,
            "credentials_available": False,
            "gmail_connected": False,
            "gmail_email": None,
            "configured": False,
            "filesystem_ok": filesystem_ok,
            "filesystem_issues": fs_issues,
            "blocking_error": container.init_error or NOT_READY_FALLBACK,
        }

    s = container.settings
    llm_ready = bool(s.llm.api_key)
    try:
        gmail = container.provider.connection_status()
    except Exception as exc:  # noqa: BLE001 - status must never break the wizard
        logger.exception("Failed to read Gmail status during setup")
        return {
            "llm_ready": llm_ready,
            "llm_provider": s.llm.provider,
            "credentials_available": False,
            "gmail_connected": False,
            "gmail_email": None,
            "configured": False,
            "filesystem_ok": filesystem_ok,
            "filesystem_issues": fs_issues,
            "blocking_error": f"Could not read Gmail status: {exc}",
        }
    return {
        "llm_ready": llm_ready,
        "llm_provider": s.llm.provider,
        "credentials_available": bool(gmail.get("credentials_available")),
        "gmail_connected": bool(gmail.get("connected")),
        "gmail_email": gmail.get("email"),
        # The app is "configured" once an LLM key exists and Gmail is connected.
        "configured": llm_ready and bool(gmail.get("connected")),
        "filesystem_ok": filesystem_ok,
        "filesystem_issues": fs_issues,
        "blocking_error": None,
    }


# --- Static frontend ---------------------------------------------------------
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
