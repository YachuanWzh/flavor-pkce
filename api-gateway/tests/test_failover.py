"""Gateway intelligent routing / failover (silent failover by default).

The upstream HTTP layer is replaced with an in-process fake so tests control
exactly which candidate route succeeds or fails.  ``verify_jwt``,
``_is_jti_revoked`` and ``_resolve_user_routing`` are stubbed so no real JWT
or auth-server is needed.
"""
import time

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
import gateway.main as gm


# ---------------------------------------------------------------------------
# Fake httpx layer
# ---------------------------------------------------------------------------

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
    """Behavior maps a URL substring to a FakeResponse or Exception."""

    behavior = {}
    requested_urls: list[str] = []
    requested_bodies: list[tuple[str, bytes]] = []

    def __init__(self, *args, **kwargs):
        pass

    def build_request(self, method, url, headers, content):
        return FakeRequest(method, url, headers, content)

    async def send(self, request, stream=True):
        FakeAsyncClient.requested_urls.append(request.url)
        FakeAsyncClient.requested_bodies.append((request.url, request.content))
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AUTH = {"Authorization": "Bearer fake-token"}


def make_jwt_payload(sub="user-1"):
    now = int(time.time())
    return {
        "sub": sub,
        "client_id": "flavor-code-cli",
        "scope": "test",
        "iat": now,
        "exp": now + 3600,
        "jti": f"jti-{sub}",
        "username": "alice",
        "config_version": 3,
        "role": "user",
    }


def primary_route(**overrides):
    value = {
        "service_name": "Primary DeepSeek",
        "api_type": "anthropic",
        "upstream_url": "https://primary.test",
        "upstream_api_key": "primary-key",
        "upstream_auth_type": "x-api-key",
        "default_model": "deepseek-v4-pro",
        "cheap_model": "deepseek-v4-flash",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "max_output_tokens": 65536,
    }
    value.update(overrides)
    return value


def fallback_route(**overrides):
    value = {
        "service_name": "Backup Route",
        "api_type": "anthropic",
        "upstream_url": "https://fallback.test",
        "upstream_api_key": "fallback-key",
        "upstream_auth_type": "x-api-key",
        "default_model": "deepseek-v4-pro",
        "cheap_model": "deepseek-v4-flash",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "max_output_tokens": 65536,
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    FakeAsyncClient.behavior = {}
    FakeAsyncClient.requested_urls = []
    FakeAsyncClient.requested_bodies = []
    gm._route_cooldowns.clear()

    # Bypass SSRF resolution for the fake hosts.
    monkeypatch.setattr(
        config, "UPSTREAM_URL_ALLOWLIST", {"primary.test", "fallback.test"},
    )
    monkeypatch.setattr(config, "FAILOVER_COOLDOWN_SECONDS", 30, raising=False)
    monkeypatch.setattr(config, "ROUTING_CACHE_TTL_SECONDS", 0, raising=False)
    # Silent failover is the default: no user confirmation, never a 409.
    monkeypatch.setattr(
        config, "FAILOVER_REQUIRE_CONSENT", False, raising=False,
    )

    monkeypatch.setattr(gm, "httpx", FakeHttpx)
    monkeypatch.setattr(gm, "verify_jwt", lambda token: make_jwt_payload())
    monkeypatch.setattr(gm, "_is_jti_revoked", _fake_not_revoked)

    yield


async def _fake_not_revoked(jti):
    return False


def _set_routing(monkeypatch, routing):
    async def fake_resolve(payload):
        return routing, None
    monkeypatch.setattr(gm, "_resolve_user_routing", fake_resolve)


@pytest.fixture
def client():
    return TestClient(gm.app, headers=AUTH)


def chat_body(model="deepseek-v4-pro"):
    return {"model": model, "messages": [{"role": "user", "content": "hi"}]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_primary_success_no_failover(client, monkeypatch):
    routing = primary_route()
    routing["fallback"] = None
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(200, b'{"id":"ok"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Primary DeepSeek"
    assert len(FakeAsyncClient.requested_urls) == 1


def test_silent_failover_on_upstream_5xx(client, monkeypatch):
    routing = primary_route()
    routing["fallback"] = fallback_route()
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"overloaded"}'),
        "fallback.test": FakeResponse(200, b'{"id":"from-backup"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Backup Route"
    # The switch itself is observable (improvement 9).
    import re
    metrics = client.get("/metrics").text
    m = re.search(
        r'gateway_route_failover_total\{route="Backup Route"\}\s+([\d.]+)',
        metrics,
    )
    assert m is not None and float(m.group(1)) >= 1


def test_silent_failover_on_connection_error(client, monkeypatch):
    import httpx
    routing = primary_route()
    routing["fallback"] = fallback_route()
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": httpx.ConnectError("connection refused"),
        "fallback.test": FakeResponse(200, b'{"id":"from-backup"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Backup Route"


def test_silent_failover_streams_sse_from_fallback(client, monkeypatch):
    routing = primary_route()
    routing["fallback"] = fallback_route()
    _set_routing(monkeypatch, routing)
    sse_body = b"event: message\ndata: {\"ok\":1}\n\n"
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(
            200, sse_body, headers={"content-type": "text/event-stream"},
        ),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Backup Route"
    assert b"event: message" in resp.content


def test_incompatible_fallback_switches_silently(client, monkeypatch):
    """Silent failover (default): a cross-protocol backup is used as-is."""
    routing = primary_route()
    routing["fallback"] = fallback_route(api_type="openai")
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"x"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Backup Route"
    assert any(
        "fallback.test" in u for u in FakeAsyncClient.requested_urls
    )


def test_incompatible_fallback_model_switches_silently(client, monkeypatch):
    """Silent failover (default): a backup missing the model is used as-is."""
    routing = primary_route()
    routing["fallback"] = fallback_route(models=["other-model-only"])
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"x"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Backup Route"


def test_incompatible_fallback_api_type_returns_409(client, monkeypatch):
    """Consent mode: a cross-protocol backup requires user confirmation."""
    monkeypatch.setattr(
        config, "FAILOVER_REQUIRE_CONSENT", True, raising=False,
    )
    routing = primary_route()
    routing["fallback"] = fallback_route(api_type="openai")
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"x"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "route_switched"
    services = [r["service_name"] for r in body["routes"]]
    assert "Primary DeepSeek" in services
    assert "Backup Route" in services
    # The incompatible fallback must NOT have been called.
    assert not any("fallback.test" in u for u in FakeAsyncClient.requested_urls)


def test_incompatible_fallback_model_returns_409(client, monkeypatch):
    """Consent mode: a backup missing the model requires user confirmation."""
    monkeypatch.setattr(
        config, "FAILOVER_REQUIRE_CONSENT", True, raising=False,
    )
    routing = primary_route()
    routing["fallback"] = fallback_route(models=["other-model-only"])
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"x"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 409
    assert resp.json()["error"] == "route_switched"


def test_all_routes_fail_returns_502(client, monkeypatch):
    routing = primary_route()
    routing["fallback"] = fallback_route()
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(503, b'{"error":"down"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 502


def test_no_fallback_single_route_failure_returns_502(client, monkeypatch):
    routing = primary_route()
    routing["fallback"] = None
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
    }
    resp = client.post("/v1/chat", json=chat_body())
    assert resp.status_code == 502


def test_preferred_route_header_overrides_incompatibility(client, monkeypatch):
    """Explicit user consent via X-Gateway-Preferred-Route allows the switch."""
    monkeypatch.setattr(
        config, "FAILOVER_REQUIRE_CONSENT", True, raising=False,
    )
    routing = primary_route()
    routing["fallback"] = fallback_route(api_type="openai")
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"consented"}'),
    }
    resp = client.post(
        "/v1/chat", json=chat_body(),
        headers={"X-Gateway-Preferred-Route": "Backup Route"},
    )
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Backup Route"


def test_preferred_route_must_belong_to_user(client, monkeypatch):
    routing = primary_route()
    routing["fallback"] = fallback_route()
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(200, b'{"id":"primary"}'),
    }
    resp = client.post(
        "/v1/chat", json=chat_body(),
        headers={"X-Gateway-Preferred-Route": "Someone-Elses-Route"},
    )
    # Unknown preferred route is ignored; primary still used.
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Primary DeepSeek"


def test_failed_route_enters_cooldown(client, monkeypatch):
    routing = primary_route()
    routing["fallback"] = fallback_route()
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"backup"}'),
    }
    first = client.post("/v1/chat", json=chat_body())
    assert first.status_code == 200

    # The failed primary route is now in cooldown: a subsequent request should
    # go straight to the fallback without re-trying the dead primary.
    calls_before = len(FakeAsyncClient.requested_urls)
    second = client.post("/v1/chat", json=chat_body())
    assert second.status_code == 200
    assert second.headers.get("X-Gateway-Route") == "Backup Route"
    new_urls = FakeAsyncClient.requested_urls[calls_before:]
    assert not any("primary.test" in u for u in new_urls)


def test_recovered_route_is_used_again_after_cooldown_expires(client, monkeypatch):
    routing = primary_route()
    routing["fallback"] = fallback_route()
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"backup"}'),
    }
    first = client.post("/v1/chat", json=chat_body())
    assert first.headers.get("X-Gateway-Route") == "Backup Route"
    # Primary is now in cooldown.
    assert any(
        deadline > time.monotonic()
        for (_, svc), deadline in gm._route_cooldowns.items()
        if svc == "Primary DeepSeek"
    )

    # Simulate the cooldown window elapsing, then the primary recovering.
    for key in list(gm._route_cooldowns):
        if key[1] == "Primary DeepSeek":
            gm._route_cooldowns[key] = time.monotonic() - 1
    FakeAsyncClient.behavior["primary.test"] = FakeResponse(200, b'{"id":"ok"}')

    second = client.post("/v1/chat", json=chat_body())
    assert second.status_code == 200
    assert second.headers.get("X-Gateway-Route") == "Primary DeepSeek"
    # A successful primary response must not leave a live cooldown behind.
    assert not any(
        deadline > time.monotonic()
        for (_, svc), deadline in gm._route_cooldowns.items()
        if svc == "Primary DeepSeek"
    )


def _forwarded_model(url_substring: str) -> str | None:
    """Model name in the body forwarded to the route matching the URL."""
    import json as _json
    for url, content in FakeAsyncClient.requested_bodies:
        if url_substring in url:
            return _json.loads(content).get("model")
    raise AssertionError(f"no request seen for {url_substring!r}")


def test_failover_rewrites_model_to_fallback_default_model(client, monkeypatch):
    """qwen3.8-flash on a dead Qwen primary must be rewritten to the
    fallback profile's main model before hitting the DeepSeek backup."""
    routing = primary_route(
        default_model="qwen3.8-flash", models=["qwen3.8-flash"],
    )
    routing["fallback"] = fallback_route(
        default_model="deepseek-v4-flash", models=["deepseek-v4-flash"],
    )
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"from-backup"}'),
    }
    resp = client.post("/v1/chat", json=chat_body(model="qwen3.8-flash"))
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Backup Route"
    # Primary got the original model; backup got the fallback's main model.
    assert _forwarded_model("primary.test") == "qwen3.8-flash"
    assert _forwarded_model("fallback.test") == "deepseek-v4-flash"


def test_failover_keeps_model_servable_by_fallback(client, monkeypatch):
    """A model the backup can already serve must be forwarded unchanged."""
    routing = primary_route(models=["qwen3.8-flash", "deepseek-v4-flash"])
    routing["fallback"] = fallback_route(
        default_model="deepseek-v4-flash", models=["deepseek-v4-flash"],
    )
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"from-backup"}'),
    }
    resp = client.post("/v1/chat", json=chat_body(model="deepseek-v4-flash"))
    assert resp.status_code == 200
    assert resp.headers.get("X-Gateway-Route") == "Backup Route"
    assert _forwarded_model("fallback.test") == "deepseek-v4-flash"


def test_failover_without_default_model_forwards_body_unchanged(
    client, monkeypatch,
):
    """Legacy fallback profiles with no main model keep the old passthrough."""
    routing = primary_route(
        default_model="qwen3.8-flash", models=["qwen3.8-flash"],
    )
    routing["fallback"] = fallback_route(
        default_model="", models=["deepseek-v4-flash"],
    )
    _set_routing(monkeypatch, routing)
    FakeAsyncClient.behavior = {
        "primary.test": FakeResponse(503, b'{"error":"down"}'),
        "fallback.test": FakeResponse(200, b'{"id":"from-backup"}'),
    }
    resp = client.post("/v1/chat", json=chat_body(model="qwen3.8-flash"))
    assert resp.status_code == 200
    assert _forwarded_model("fallback.test") == "qwen3.8-flash"
