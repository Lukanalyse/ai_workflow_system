"""PKCE / OAuth flow regression tests.

Guards against the "invalid_grant: Missing code verifier" bug: the PKCE
``code_verifier`` generated when the consent URL is built MUST be replayed,
unchanged, on the token exchange — even across a process restart (Docker).

A ``FakeFlow`` stands in for ``google_auth_oauthlib``'s ``Flow`` so the test
never touches the network. It mimics the real behaviour: ``authorization_url``
auto-generates ``code_verifier`` and ``fetch_token`` sends whatever
``code_verifier`` is attached at exchange time.
"""

from __future__ import annotations

import secrets

import pytest

from app.auth.oauth_flow import GmailOAuthFlow

# Records the verifier present on the flow at fetch_token() time, across the
# two distinct FakeFlow instances (connect builds one, callback builds another).
EXCHANGE_LOG: dict[str, str | None] = {}
FIXED_STATE = "STATE-abcdef123456"


class FakeFlow:
    def __init__(self):
        self.code_verifier = None  # mirrors Flow(autogenerate_code_verifier=True)
        self.credentials = object()

    def authorization_url(self, **kwargs):
        # Real lib auto-generates the verifier here and embeds the challenge.
        self.code_verifier = "VERIFIER-" + secrets.token_urlsafe(16)
        return ("https://accounts.google.com/o/oauth2/v2/auth?mock=1", FIXED_STATE)

    def fetch_token(self, code=None, **kwargs):
        # Capture the verifier the way Google's token endpoint would receive it.
        EXCHANGE_LOG["verifier_at_exchange"] = self.code_verifier
        EXCHANGE_LOG["code"] = code


@pytest.fixture(autouse=True)
def _patch_flow(monkeypatch, tmp_path):
    EXCHANGE_LOG.clear()
    # credentials file must exist for _build_flow().
    creds = tmp_path / "credentials.json"
    creds.write_text('{"installed": {"client_id": "x"}}', encoding="utf-8")

    def fake_from_client_secrets_file(*args, **kwargs):
        return FakeFlow()

    monkeypatch.setattr(
        "google_auth_oauthlib.flow.Flow.from_client_secrets_file",
        staticmethod(fake_from_client_secrets_file),
    )
    # Avoid the real Gmail profile call.
    monkeypatch.setattr(GmailOAuthFlow, "_fetch_email", staticmethod(lambda creds: "user@example.com"))
    return creds


def _make_flow(creds, tmp_path):
    return GmailOAuthFlow(
        credentials_path=creds,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri="http://localhost:3000/api/gmail/callback",
        state_path=tmp_path / "storage" / "oauth_state.json",
    )


def test_verifier_is_persisted_at_connect(_patch_flow, tmp_path):
    flow = _make_flow(_patch_flow, tmp_path)
    flow.authorization_url()
    pending = flow._pending[FIXED_STATE]
    assert pending.code_verifier.startswith("VERIFIER-")
    # Persisted to disk so the callback can find it.
    state_file = tmp_path / "storage" / "oauth_state.json"
    assert state_file.exists()
    assert pending.code_verifier in state_file.read_text(encoding="utf-8")


def test_same_verifier_replayed_at_exchange(_patch_flow, tmp_path):
    flow = _make_flow(_patch_flow, tmp_path)
    flow.authorization_url()
    original = flow._pending[FIXED_STATE].code_verifier

    creds, email = flow.exchange_code(code="auth-code-xyz", state=FIXED_STATE)
    assert email == "user@example.com"
    # The exact verifier from /connect must reach fetch_token (the bug fix).
    assert EXCHANGE_LOG["verifier_at_exchange"] == original
    assert EXCHANGE_LOG["code"] == "auth-code-xyz"


def test_verifier_survives_restart(_patch_flow, tmp_path):
    # Connect on one instance, callback on a fresh instance (simulates a Docker
    # restart or a different uvicorn worker handling the callback).
    flow1 = _make_flow(_patch_flow, tmp_path)
    flow1.authorization_url()
    original = flow1._pending[FIXED_STATE].code_verifier

    flow2 = _make_flow(_patch_flow, tmp_path)  # cold start: in-memory dict empty
    assert flow2._pending  # loaded from disk
    flow2.exchange_code(code="auth-code-xyz", state=FIXED_STATE)
    assert EXCHANGE_LOG["verifier_at_exchange"] == original


def test_unknown_state_is_rejected(_patch_flow, tmp_path):
    flow = _make_flow(_patch_flow, tmp_path)
    flow.authorization_url()
    with pytest.raises(PermissionError):
        flow.exchange_code(code="x", state="not-the-real-state")


def test_state_consumed_once(_patch_flow, tmp_path):
    flow = _make_flow(_patch_flow, tmp_path)
    flow.authorization_url()
    flow.exchange_code(code="x", state=FIXED_STATE)
    # Replaying the same state must fail (entry consumed + persisted removal).
    with pytest.raises(PermissionError):
        flow.exchange_code(code="x", state=FIXED_STATE)


def test_state_file_has_private_permissions(_patch_flow, tmp_path):
    import os
    import stat

    flow = _make_flow(_patch_flow, tmp_path)
    flow.authorization_url()
    state_file = tmp_path / "storage" / "oauth_state.json"
    if os.name == "posix":
        mode = stat.S_IMODE(state_file.stat().st_mode)
        assert mode & 0o077 == 0  # no group/other access
