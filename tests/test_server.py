"""Web-layer robustness + security tests.

Verifies that:
* a degraded (broken-filesystem) startup never returns a traceback,
* health / setup-status expose the filesystem problem,
* service endpoints fail closed with an actionable 503,
* secrets are masked and never leak through settings/setup/gmail responses.
"""

from __future__ import annotations

import types

import pytest
from fastapi.testclient import TestClient

import web.server as srv
from app.config.settings import AppSettings
from app.security.fs_validation import PathIssue


@pytest.fixture()
def client():
    # raise_server_exceptions=False so the catch-all handler is exercised
    # exactly as a real client would see it (clean 500, no traceback).
    return TestClient(srv.app, raise_server_exceptions=False)


@pytest.fixture()
def restore_container():
    snapshot = {k: getattr(srv.container, k) for k in vars(srv.container)}
    try:
        yield srv.container
    finally:
        for k, v in snapshot.items():
            setattr(srv.container, k, v)


def _make_degraded(container, *, code="wrong_type_dir"):
    container.degraded = True
    container.settings = None
    container.provider = None
    container.init_error = (
        ".env exists but is a directory. Remove it and create a file named .env."
    )
    container.fs_issues = [
        PathIssue(
            name=".env",
            path=".env",
            kind="file",
            code=code,
            severity="error",
            message=container.init_error,
        )
    ]


# --- Degraded mode: no traceback, fs issues exposed --------------------------
def test_health_degraded_no_crash(client, restore_container):
    _make_degraded(restore_container)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["filesystem"]["status"] == "error"
    assert any(i["code"] == "wrong_type_dir" for i in body["filesystem"]["issues"])
    # Services reported as unknown, not crashed.
    assert body["gmail"]["status"] == "unknown"


def test_setup_status_degraded_no_crash(client, restore_container):
    _make_degraded(restore_container)
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["filesystem_ok"] is False
    assert body["configured"] is False
    assert "directory" in body["blocking_error"]
    assert body["filesystem_issues"]


def test_service_endpoints_fail_closed_503(client, restore_container):
    _make_degraded(restore_container)
    for path in ("/api/emails", "/api/config", "/api/settings", "/api/gmail/status"):
        r = client.get(path)
        assert r.status_code == 503, path
        assert "directory" in r.json()["detail"]


def test_setup_status_never_500_even_if_provider_raises(client, restore_container):
    c = restore_container
    c.degraded = False
    c.settings = AppSettings(openai_api_key="sk-secret-ABCD1234")

    class _BoomProvider:
        def connection_status(self):
            raise RuntimeError("boom /secret/path leak attempt")

    c.provider = _BoomProvider()
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["gmail_connected"] is False


# --- Security: masking + no secret leakage -----------------------------------
def test_mask_helper():
    assert srv._mask("sk-secret-ABCD1234").endswith("1234")
    assert "secret" not in srv._mask("sk-secret-ABCD1234")
    assert srv._mask("") == ""
    assert srv._mask("abc") == "set"  # too short to reveal a suffix


def test_settings_response_masks_keys(client, restore_container):
    c = restore_container
    c.degraded = False
    c.settings = AppSettings(
        openai_api_key="sk-secret-ABCD1234",
        anthropic_api_key="sk-ant-secret-WXYZ9876",
    )
    r = client.get("/api/settings")
    assert r.status_code == 200
    blob = r.text
    # Raw secrets must never appear in the response payload.
    assert "sk-secret-ABCD1234" not in blob
    assert "sk-ant-secret-WXYZ9876" not in blob
    body = r.json()
    assert body["openai_api_key"].startswith("…") or body["openai_api_key"] == "set"


def test_setup_status_carries_no_secret(client, restore_container):
    c = restore_container
    c.degraded = False
    c.settings = AppSettings(openai_api_key="sk-secret-ABCD1234")

    class _FakeProvider:
        def connection_status(self):
            return {
                "connected": True,
                "email": "user@example.com",
                "credentials_available": True,
            }

    c.provider = _FakeProvider()
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    assert "sk-secret-ABCD1234" not in r.text
    # llm_ready must be a boolean, never the key itself.
    assert isinstance(r.json()["llm_ready"], bool)
