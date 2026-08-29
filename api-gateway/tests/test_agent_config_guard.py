"""Agent endpoints translate legacy-fallback credential errors (improvement 2).

A token without ``config_version`` resolves to the gateway-wide legacy
fallback routing. When that fallback's credentials are rejected by the
upstream (401/403), the agent endpoints must surface an actionable
``llm_config_required`` error instead of an opaque 502.
"""

import os
import tempfile
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="guard_api_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    old_token = config.AUDIT_API_TOKEN
    config.AUDIT_DB_PATH = tmp
    config.AUDIT_API_TOKEN = "test-audit-token"
    init_audit_db()
    yield
    config.AUDIT_DB_PATH = old
    config.AUDIT_API_TOKEN = old_token
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.fixture(autouse=True)
def setup_keys():
    keys_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-server", "keys"
    )
    os.makedirs(keys_dir, exist_ok=True)
    import sys
    sys.path.insert(0, os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-server",
    ))
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()
    config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
    config.UPSTREAM_URL = "https://upstream.invalid"
    config.UPSTREAM_API_KEY = ""
    yield


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


AUTH = {"X-Audit-Token": "test-audit-token"}


def _fake_status_post(status: int):
    class _R:
        status_code = status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "nope", request=None, response=self,
                )

        def json(self):
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        return _R()

    return fake_post


def test_ask_upstream_401_becomes_llm_config_required(client):
    with patch(
        "gateway.agent.httpx.AsyncClient.post",
        new=_fake_status_post(401),
    ):
        resp = client.post(
            "/api/agent/ask", headers=AUTH, json={"question": "count"},
        )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "llm_config_required" in detail
    assert "LLM 设置" in detail


def test_ask_upstream_500_stays_generic_502(client):
    with patch(
        "gateway.agent.httpx.AsyncClient.post",
        new=_fake_status_post(500),
    ):
        resp = client.post(
            "/api/agent/ask", headers=AUTH, json={"question": "count"},
        )
    assert resp.status_code == 502


def test_chat_stream_401_carries_llm_config_code(client):
    async def fake_turn(question, session=None, routing=None, user=None,
                        user_id=None):
        class _R:
            status_code = 403

        raise httpx.HTTPStatusError("denied", request=None, response=_R())
        yield {}  # pragma: no cover — makes this an async generator

    with patch("gateway.agent_loop.run_agent_turn", new=fake_turn):
        resp = client.post(
            "/api/agent/chat", headers=AUTH, json={"message": "hi"},
        )
    assert resp.status_code == 200  # SSE established, error is an event
    assert '"code":"llm_config_required"' in resp.text
    assert "LLM 设置" in resp.text


def test_ask_stream_401_carries_llm_config_code(client):
    async def fake_stream_agent(question, routing=None, user="-",
                                user_id=None, history=None):
        class _R:
            status_code = 401

        raise httpx.HTTPStatusError("denied", request=None, response=_R())
        yield {}  # pragma: no cover

    with patch("gateway.agent.stream_agent", new=fake_stream_agent):
        resp = client.post(
            "/api/agent/ask/stream", headers=AUTH,
            json={"question": "how many?"},
        )
    assert '"code":"llm_config_required"' in resp.text
