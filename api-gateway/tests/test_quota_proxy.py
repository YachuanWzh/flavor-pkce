"""Proxy enforcement of per-user quota/budget (gateway.quota).

The upstream HTTP layer is replaced with an in-process fake (same approach
as test_failover) so no real network is involved. ``verify_jwt`` and
``_resolve_user_routing`` are stubbed; ``AUDIT_DB_PATH`` points at a temp
file so quota_usage rows are real SQLite data.
"""
import os
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
import gateway.main as gm
import gateway.quota as quota
from gateway.database import init_audit_db


class FakeResponse:
    def __init__(self, status_code=200, content=b"{}", headers=None):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {"content-type": "application/json"}

    async def aread(self):
        return self._content

    def aiter_bytes(self):
        async def gen():
            yield self._content
        return gen()

    async def aclose(self):
        pass


class FakeRequest:
    def __init__(self, method, url, headers, content):
        self.method = method
        self.url = url
        self.headers = headers
        self.content = content


class FakeAsyncClient:
    behavior = {}
    requested_urls: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def build_request(self, method, url, headers, content):
        return FakeRequest(method, url, headers, content)

    async def send(self, request, stream=True):
        FakeAsyncClient.requested_urls.append(request.url)
        for key, value in FakeAsyncClient.behavior.items():
            if key in request.url:
                if isinstance(value, Exception):
                    raise value
                return value
        return FakeResponse(status_code=200, content=b'{"ok":true}')

    async def aclose(self):
        pass


class FakeHttpx:
    AsyncClient = FakeAsyncClient
    HTTPError = __import__("httpx").HTTPError


AUTH = {"Authorization": "Bearer fake-token"}


def make_jwt_payload(sub="user-1", username="alice"):
    now = int(time.time())
    return {
        "sub": sub,
        "client_id": "flavor-code-cli",
        "scope": "test",
        "iat": now,
        "exp": now + 3600,
        "jti": f"jti-{sub}",
        "username": username,
        "config_version": 3,
        "role": "user",
    }


def routing(**overrides):
    value = {
        "service_name": "Primary",
        "api_type": "openai",
        "upstream_url": "https://primary.test",
        "upstream_api_key": "primary-key",
        "upstream_auth_type": "bearer",
        "default_model": "gpt-x",
        "cheap_model": "gpt-x",
        "models": ["gpt-x"],
        "max_output_tokens": 4096,
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="quota_proxy_test_")
    os.close(fd)
    old_db = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    init_audit_db()

    FakeAsyncClient.behavior = {}
    FakeAsyncClient.requested_urls = []
    gm._route_cooldowns.clear()

    monkeypatch.setattr(
        config, "UPSTREAM_URL_ALLOWLIST", {"primary.test"}, raising=False,
    )
    monkeypatch.setattr(config, "ROUTING_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(config, "RATE_LIMIT_RPM", 0, raising=False)
    monkeypatch.setattr(config, "DAILY_TOKEN_BUDGET", 0, raising=False)
    monkeypatch.setattr(config, "DAILY_COST_BUDGET_USD", 0.0, raising=False)
    monkeypatch.setattr(config, "MODEL_PRICES", {}, raising=False)

    monkeypatch.setattr(gm, "httpx", FakeHttpx)
    monkeypatch.setattr(gm, "verify_jwt", lambda token: make_jwt_payload())
    monkeypatch.setattr(gm, "_is_jti_revoked", _fake_not_revoked)

    async def fake_resolve(payload):
        return routing(), None
    monkeypatch.setattr(gm, "_resolve_user_routing", fake_resolve)

    yield
    config.AUDIT_DB_PATH = old_db
    if os.path.exists(tmp):
        os.remove(tmp)


async def _fake_not_revoked(jti):
    return False


@pytest.fixture
def client():
    return TestClient(gm.app, headers=AUTH)


def chat_body(model="gpt-x"):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}]}


USAGE_200 = FakeResponse(200, (
    b'{"id":"c1","model":"gpt-x","choices":[],"usage":'
    b'{"prompt_tokens":100,"completion_tokens":50}}'
))


def test_no_quota_configured_requests_pass_freely(client):
    FakeAsyncClient.behavior = {"primary.test": USAGE_200}
    for _ in range(10):
        assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    assert len(FakeAsyncClient.requested_urls) == 10


def test_rate_limit_returns_429(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_RPM", 2, raising=False)
    FakeAsyncClient.behavior = {"primary.test": USAGE_200}
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    resp = client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert "retry_after_seconds" in resp.json()
    # Only the two admitted requests reached the upstream.
    assert len(FakeAsyncClient.requested_urls) == 2


def test_token_budget_returns_429(client, monkeypatch):
    monkeypatch.setattr(config, "DAILY_TOKEN_BUDGET", 150, raising=False)
    FakeAsyncClient.behavior = {"primary.test": USAGE_200}  # 150 tokens/call
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    usage = quota.get_day_usage("alice")
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50
    resp = client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 429
    assert resp.json()["error"] == "token_budget_exceeded"


def test_cost_budget_returns_429(client, monkeypatch):
    monkeypatch.setattr(config, "MODEL_PRICES", {
        "gpt-x": {"prompt": 2.0, "completion": 4.0},
    }, raising=False)
    monkeypatch.setattr(config, "DAILY_COST_BUDGET_USD", 0.001, raising=False)
    # 1M prompt tokens @ $2/M = $2 — one call exhausts the $0.001 budget.
    big = FakeResponse(200, (
        b'{"id":"c2","model":"gpt-x","choices":[],"usage":'
        b'{"prompt_tokens":1000000,"completion_tokens":0}}'
    ))
    FakeAsyncClient.behavior = {"primary.test": big}
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    resp = client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 429
    assert resp.json()["error"] == "cost_budget_exceeded"


def test_quota_is_per_user(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_LIMIT_RPM", 1, raising=False)
    FakeAsyncClient.behavior = {"primary.test": USAGE_200}
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 429
    # A different user is unaffected.
    monkeypatch.setattr(gm, "verify_jwt",
                        lambda token: make_jwt_payload(sub="user-2", username="bob"))
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200


def test_failed_upstream_does_not_consume_token_budget(client, monkeypatch):
    """Usage accounting runs only when the upstream reported usage."""
    monkeypatch.setattr(config, "DAILY_TOKEN_BUDGET", 100, raising=False)
    FakeAsyncClient.behavior = {"primary.test": FakeResponse(200, b'{}')}
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200
    assert quota.get_day_usage("alice")["prompt_tokens"] == 0
    # Still under budget: next request admitted.
    assert client.post("/v1/chat/completions", json=chat_body()).status_code == 200


def test_quota_headers_report_usage(client, monkeypatch):
    monkeypatch.setattr(config, "DAILY_TOKEN_BUDGET", 1000, raising=False)
    FakeAsyncClient.behavior = {"primary.test": USAGE_200}
    resp = client.post("/v1/chat/completions", json=chat_body())
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Daily-Token-Budget") == "1000"
    assert resp.headers.get("X-Gateway-Daily-Tokens-Used") == "150"
