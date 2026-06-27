"""Robust, thread-safe HTTP layer for the Gmail API.

Root cause this module fixes: ``googleapiclient`` builds a service backed by a
single ``httplib2.Http`` (one TLS connection pool), and FastAPI runs the sync
endpoints in a threadpool. Sharing that one non-thread-safe client across worker
threads interleaves TLS records on the same socket — which surfaces as
``SSLError`` (WRONG_VERSION_NUMBER / MIXED_HANDSHAKE) and 60s read timeouts,
especially during the burst of calls when an Archive folder is opened.

The fix, in order of the agreed plan:
  A. thread-safe transport — a *thread-local* AuthorizedHttp, so each worker
     thread reuses its own keep-alive connection but never shares one.
  B. explicit, shorter timeout (fail fast instead of hanging 60s).
  C. retry with exponential backoff + jitter on *transient* errors only.
  D. ThreadSafeCredentials — serialise token refresh (also not thread-safe).
  E. per-operation timing + retry logging (instrumentation).
"""

from __future__ import annotations

import logging
import random
import socket
import ssl
import threading
import time

logger = logging.getLogger(__name__)

# B — per-connection connect+read timeout (the implicit default is 60s).
GMAIL_HTTP_TIMEOUT = 30
# C — retry policy.
RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 8.0
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# E — log INFO when one Gmail op is unusually slow; DEBUG otherwise.
SLOW_OP_MS = 3000


def _build_http():
    from googleapiclient.http import build_http

    http = build_http()
    http.timeout = GMAIL_HTTP_TIMEOUT
    return http


def make_authorized_http(credentials):
    """A fresh AuthorizedHttp (own connection pool) for the given credentials."""
    import google_auth_httplib2

    return google_auth_httplib2.AuthorizedHttp(credentials, http=_build_http())


class ThreadSafeCredentials:
    """Serialises token refresh across threads (google-auth refresh is not
    thread-safe); everything else delegates to the wrapped credentials.

    Without this, a concurrent burst that hits an expired token would fire
    several overlapping refreshes against the token endpoint.
    """

    def __init__(self, credentials) -> None:
        object.__setattr__(self, "_creds", credentials)
        object.__setattr__(self, "_lock", threading.Lock())

    def before_request(self, request, method, url, headers):
        # Hot path stays lock-free; only an actual refresh is serialised
        # (double-checked) so concurrent requests with a valid token don't
        # contend, but an expired token triggers exactly one refresh.
        if not self._creds.valid:
            self.refresh(request)
        self._creds.before_request(request, method, url, headers)

    def refresh(self, request):
        with self._lock:
            if not self._creds.valid:
                self._creds.refresh(request)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_creds"), name)


# Register as a virtual subclass of google-auth Credentials so googleapiclient's
# isinstance() checks (e.g. _auth.is_valid in the batch path) take the modern
# google-auth branch — which reads ``.valid`` — instead of the legacy
# oauth2client branch that expects an ``access_token`` attribute.
try:
    from google.auth.credentials import Credentials as _GoogleCredentials

    _GoogleCredentials.register(ThreadSafeCredentials)
except Exception:  # noqa: BLE001 - registration is best-effort
    pass


def build_gmail_service(credentials):
    """Build a Gmail service whose every request uses a thread-local HTTP client.

    Returns ``(service, http_factory)``. ``http_factory()`` yields the calling
    thread's AuthorizedHttp and is also used for ``BatchHttpRequest.execute`` so
    batches never fall back to the shared default client.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import HttpRequest

    safe = ThreadSafeCredentials(credentials)
    thread_local = threading.local()

    def http_factory():
        http = getattr(thread_local, "http", None)
        if http is None:
            http = make_authorized_http(safe)
            thread_local.http = http
        return http

    def _request_builder(http, *args, **kwargs):
        # Ignore the default http googleapiclient passes; use this thread's own.
        return HttpRequest(http_factory(), *args, **kwargs)

    service = build(
        "gmail",
        "v1",
        credentials=credentials,
        requestBuilder=_request_builder,
        static_discovery=True,
        cache_discovery=False,
    )
    return service, http_factory


def is_transient(exc: BaseException) -> bool:
    """Whether an error is a temporary network/server condition worth retrying.

    Deliberately conservative: auth/permission and 4xx (except 429) are *not*
    retried, so a missing-scope ``PermissionError`` still surfaces immediately.
    """
    from googleapiclient.errors import HttpError

    if isinstance(exc, HttpError):
        status = getattr(exc, "status_code", None)
        if status is None and getattr(exc, "resp", None) is not None:
            status = getattr(exc.resp, "status", None)
        return status in _RETRYABLE_STATUS
    if isinstance(
        exc,
        (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError,
         BrokenPipeError, ConnectionResetError),
    ):
        return True
    try:
        import httplib2

        if isinstance(exc, httplib2.HttpLib2Error):
            return True
    except Exception:  # noqa: BLE001 - import guard only
        pass
    try:
        from google.auth.exceptions import TransportError

        if isinstance(exc, TransportError):
            return True
    except Exception:  # noqa: BLE001 - import guard only
        pass
    # Low-level socket/OS errors are transient — but a missing-scope
    # PermissionError (also an OSError subclass) must never be retried.
    if isinstance(exc, OSError) and not isinstance(exc, PermissionError):
        return True
    return False


def _log_timing(op: str, start: float, attempt: int) -> None:
    ms = (time.perf_counter() - start) * 1000
    suffix = f" (attempt {attempt + 1})" if attempt else ""
    if ms >= SLOW_OP_MS:
        logger.info("Gmail %s took %.0fms%s", op, ms, suffix)
    else:
        logger.debug("Gmail %s ok in %.0fms%s", op, ms, suffix)


def _run_with_retry(fn, *, op: str):
    last: BaseException | None = None
    for attempt in range(RETRY_ATTEMPTS):
        start = time.perf_counter()
        try:
            result = fn()
            _log_timing(op, start, attempt)
            return result
        except Exception as exc:  # noqa: BLE001 - classified by is_transient
            last = exc
            if not is_transient(exc) or attempt == RETRY_ATTEMPTS - 1:
                raise
            delay = min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** attempt))
            delay += random.uniform(0, RETRY_BASE_DELAY)  # jitter
            logger.warning(
                "Gmail %s: transient %s (attempt %d/%d) — retrying in %.2fs",
                op, type(exc).__name__, attempt + 1, RETRY_ATTEMPTS, delay,
            )
            time.sleep(delay)
    raise last  # pragma: no cover - loop always returns or raises above


def execute(request, *, op: str = "request"):
    """Execute a googleapiclient request with retry/backoff + timing."""
    return _run_with_retry(request.execute, op=op)


def execute_batch(batch, *, http=None, op: str = "batch"):
    """Execute a BatchHttpRequest (with an explicit http when provided)."""
    fn = (lambda: batch.execute(http=http)) if http is not None else batch.execute
    return _run_with_retry(fn, op=op)
