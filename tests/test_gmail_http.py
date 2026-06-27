"""Phase 'Gmail robustness' tests: transient-error classification + retry policy.

These cover the retry/backoff wrapper without any network: a fake request whose
``execute`` raises a controlled sequence.
"""

from __future__ import annotations

import socket
import ssl

import pytest

from app.email import gmail_http


def _http_error(status: int):
    from googleapiclient.errors import HttpError

    resp = type("Resp", (), {"status": status, "reason": "x"})()
    return HttpError(resp, b"{}")


# --- is_transient -----------------------------------------------------------
def test_transient_network_errors_are_retryable() -> None:
    assert gmail_http.is_transient(ssl.SSLError("record layer failure"))
    assert gmail_http.is_transient(socket.timeout("timed out"))
    assert gmail_http.is_transient(TimeoutError("The read operation timed out"))
    assert gmail_http.is_transient(ConnectionResetError())
    assert gmail_http.is_transient(BrokenPipeError())


def test_retryable_http_statuses() -> None:
    for status in (429, 500, 502, 503, 504):
        assert gmail_http.is_transient(_http_error(status)), status


def test_non_transient_errors_are_not_retryable() -> None:
    assert not gmail_http.is_transient(_http_error(404))
    assert not gmail_http.is_transient(_http_error(403))
    assert not gmail_http.is_transient(ValueError("bad input"))
    # Missing-scope PermissionError (an OSError subclass) must never be retried.
    assert not gmail_http.is_transient(PermissionError("reconnect Gmail"))


# --- retry behaviour --------------------------------------------------------
class _FlakyRequest:
    def __init__(self, errors: list[Exception], result="ok"):
        self._errors = list(errors)
        self._result = result
        self.calls = 0

    def execute(self):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self._result


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gmail_http.time, "sleep", lambda *_: None)


def test_execute_retries_transient_then_succeeds() -> None:
    req = _FlakyRequest([ssl.SSLError("x"), TimeoutError("y")], result="done")
    assert gmail_http.execute(req, op="t") == "done"
    assert req.calls == 3  # two failures + one success


def test_execute_does_not_retry_non_transient() -> None:
    req = _FlakyRequest([ValueError("nope")])
    with pytest.raises(ValueError):
        gmail_http.execute(req, op="t")
    assert req.calls == 1  # no retry


def test_execute_gives_up_after_max_attempts() -> None:
    req = _FlakyRequest([ssl.SSLError("x")] * 10)
    with pytest.raises(ssl.SSLError):
        gmail_http.execute(req, op="t")
    assert req.calls == gmail_http.RETRY_ATTEMPTS


def test_execute_batch_passes_http_through() -> None:
    seen = {}

    class _Batch:
        def execute(self, http=None):
            seen["http"] = http
            return "batched"

    sentinel = object()
    assert gmail_http.execute_batch(_Batch(), http=sentinel, op="b") == "batched"
    assert seen["http"] is sentinel


def test_execute_batch_without_http_calls_plain_execute() -> None:
    class _Batch:
        def execute(self):  # no http kwarg accepted (mirrors test fakes)
            return "ok"

    assert gmail_http.execute_batch(_Batch(), http=None, op="b") == "ok"
