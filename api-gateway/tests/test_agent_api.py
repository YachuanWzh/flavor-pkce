"""API tests for /api/agent/ask."""

import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_log


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="agent_api_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    old_token = config.AUDIT_API_TOKEN
    config.AUDIT_DB_PATH = tmp
    config.AUDIT_API_TOKEN = "test-audit-token"
    init_audit_db()
    insert_log(
        timestamp="2026-08-01T10:00:00+00:00",
        user="alice", method="POST", path="/v1/messages",
        status=200, duration_ms=500.0, upstream_ms=450.0, level="INFO",
        prompt_tokens=150, completion_tokens=80, model="claude-sonnet-4-5",
    )
    yield
    config.AUDIT_DB_PATH = old
    config.AUDIT_API_TOKEN = old_token
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.fixture(autouse=True)
def setup_keys():
    # auth_server.config BASE_DIR is auth-server/, so keys live at
    # auth-server/keys/{private,public}.pem (not auth_server/keys/).
    keys_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-server", "keys"
    )
    os.makedirs(keys_dir, exist_ok=True)
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auth-server"))
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()
    config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
    config.UPSTREAM_URL = "https://httpbin.org"
    config.UPSTREAM_API_KEY = ""
    yield


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


AUTH = {"X-Audit-Token": "test-audit-token"}


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json_body = json_body
        self.calls: list[dict] = []

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                "Upstream error", request=None, response=self,
            )

    def json(self):
        return self._json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_post(fake: _FakeResponse):
    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        fake.calls.append({"url": url, "headers": headers or {}, "json": json})
        return fake
    return fake_post


def test_requires_auth(client):
    resp = client.post("/api/agent/ask", json={"question": "count requests"})
    assert resp.status_code == 401


def test_ask_ok(client):
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": '```sql\nSELECT COUNT(*) AS n FROM audit_logs\n```'}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        resp = client.post(
            "/api/agent/ask",
            headers=AUTH,
            json={"question": "How many requests total?"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["sql"].startswith("SELECT COUNT(*)")
    assert data["rows"] == [{"n": 1}]
    assert data["truncated"] is False


def test_ask_empty_question(client):
    resp = client.post("/api/agent/ask", headers=AUTH, json={"question": "   "})
    assert resp.status_code == 400


def test_ask_upstream_failure(client):
    fake = _FakeResponse(502, {"error": "boom"})
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        resp = client.post("/api/agent/ask", headers=AUTH, json={"question": "count"})
    assert resp.status_code == 502


def test_ask_uses_signed_in_users_llm_config(client):
    """Regression: /api/agent/ask must call the upstream with the JWT user's
    own LLM config (from the internal llm-config endpoint), not the
    gateway-wide UPSTREAM_API_KEY env."""
    from auth_server.jwt_utils import create_jwt

    token = create_jwt(
        sub="user-abc",
        client_id="test-client",
        username="alice",
        config_version=1,
        role="admin",
    )
    user_routing = {
        "upstream_url": "https://api.deepseek.com/anthropic",
        "upstream_api_key": "user-secret-key",
        "upstream_auth_type": "x-api-key",
        "default_model": "user-model",
        "api_type": "anthropic",
    }
    fake = _FakeResponse(200, {
        "content": [{"type": "text", "text": "SELECT COUNT(*) AS n FROM audit_logs"}]
    })

    async def fake_get(self, url, *, params=None, headers=None):
        class _R:
            status_code = 200

            def json(self):
                return user_routing

        return _R()

    with (
        patch("gateway.main.httpx.AsyncClient.get", new=fake_get),
        patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)),
    ):
        resp = client.post(
            "/api/agent/ask",
            headers={"Authorization": f"Bearer {token}"},
            json={"question": "how many?"},
        )

    assert resp.status_code == 200
    call = fake.calls[-1]
    assert call["url"] == "https://api.deepseek.com/anthropic/v1/messages"
    assert call["headers"].get("x-api-key") == "user-secret-key"
    assert call["json"]["model"] == "user-model"
