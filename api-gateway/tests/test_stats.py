"""Report-data tests: cache/service columns, cache-token extraction, /api/stats.

These drive three capabilities:
1. audit_logs grows cache_read_tokens / cache_creation_tokens / service_name.
2. Token extraction captures provider prompt-cache fields and recovers the
   full usage (including completion tokens) from streamed SSE responses.
3. Aggregation endpoints under /api/stats power the report dashboard.
"""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_log, query_logs
from gateway.stats import cache_usage


# ---------------------------------------------------------------------------
# Database: new report columns
# ---------------------------------------------------------------------------

class TestReportColumns:

    @pytest.fixture(autouse=True)
    def setup(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db", prefix="stats_test_")
        os.close(fd)
        self._old = config.AUDIT_DB_PATH
        config.AUDIT_DB_PATH = self._tmp
        init_audit_db()
        yield
        config.AUDIT_DB_PATH = self._old
        if os.path.exists(self._tmp):
            os.remove(self._tmp)

    def test_cache_and_service_columns_persisted(self):
        insert_log(
            timestamp="2026-08-01T10:00:00+00:00",
            user="alice", method="POST", path="/v1/messages",
            status=200, duration_ms=500.0, upstream_ms=450.0, level="INFO",
            prompt_tokens=150, completion_tokens=80, model="claude-sonnet-4-5",
            cache_read_tokens=100, cache_creation_tokens=20,
            service_name="anthropic-main", user_id="uuid-alice",
        )
        item = query_logs({"page": 1, "page_size": 1})["items"][0]
        assert item["cache_read_tokens"] == 100
        assert item["cache_creation_tokens"] == 20
        assert item["service_name"] == "anthropic-main"
        assert item["user_id"] == "uuid-alice"

    def test_new_columns_null_by_default(self):
        insert_log(
            timestamp="2026-08-01T10:01:00+00:00",
            user="bob", method="GET", path="/v1/models",
            status=200, duration_ms=10.0, upstream_ms=None, level="INFO",
        )
        item = query_logs({"page": 1, "page_size": 1})["items"][0]
        assert item["cache_read_tokens"] is None
        assert item["cache_creation_tokens"] is None
        assert item["service_name"] is None
        assert item["user_id"] is None


# ---------------------------------------------------------------------------
# Extraction: provider prompt-cache fields
# ---------------------------------------------------------------------------

class TestCacheTokenExtraction:

    def _run(self, body: bytes):
        from unittest.mock import Mock
        from gateway.main import _extract_full_token_usage
        req = Mock()
        _extract_full_token_usage(req, body)
        return req.state

    def test_anthropic_cache_fields(self):
        body = (
            b'{"usage":{"input_tokens":25,"output_tokens":321,'
            b'"cache_creation_input_tokens":1500,"cache_read_input_tokens":7123}}'
        )
        state = self._run(body)
        assert state.prompt_tokens == 25
        assert state.completion_tokens == 321
        assert state.cache_read_tokens == 7123
        assert state.cache_creation_tokens == 1500

    def test_openai_cached_tokens(self):
        body = (
            b'{"usage":{"prompt_tokens":200,"completion_tokens":50,'
            b'"input_tokens_details":{"cached_tokens":128}}}'
        )
        state = self._run(body)
        assert state.cache_read_tokens == 128

    def test_cache_fields_default_none(self):
        state = self._run(b'{"usage":{"prompt_tokens":10,"completion_tokens":5}}')
        assert state.cache_read_tokens is None
        assert state.cache_creation_tokens is None


class TestSseUsageFinalization:
    """Full-stream usage recovery from collected SSE chunks."""

    def _run(self, body: bytes):
        from unittest.mock import Mock
        from gateway.main import _extract_usage_from_stream
        req = Mock()
        _extract_usage_from_stream(req, body)
        return req.state

    def test_anthropic_stream_full_usage(self):
        body = (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"usage":'
            b'{"input_tokens":25,"cache_creation_input_tokens":1500,'
            b'"cache_read_input_tokens":7123}}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":321}}\n\n'
        )
        state = self._run(body)
        assert state.prompt_tokens == 25
        assert state.completion_tokens == 321
        assert state.cache_read_tokens == 7123
        assert state.cache_creation_tokens == 1500

    def test_openai_stream_final_usage(self):
        body = (
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":200,'
            b'"completion_tokens":50,'
            b'"input_tokens_details":{"cached_tokens":128}}}\n\n'
            b'data: [DONE]\n\n'
        )
        state = self._run(body)
        assert state.prompt_tokens == 200
        assert state.completion_tokens == 50
        assert state.cache_read_tokens == 128

    def test_no_usage_events(self):
        state = self._run(b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n')
        assert state.completion_tokens is None

    def test_garbage_input(self):
        state = self._run(b'not sse at all')
        assert state.prompt_tokens is None
        assert state.completion_tokens is None


# ---------------------------------------------------------------------------
# /api/stats aggregation endpoints
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="stats_api_test_")
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
        os.path.dirname(__file__), "..", "..", "auth-server", "auth_server", "keys"
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


def _seed_usage():
    insert_log(
        timestamp="2026-08-01T09:00:00+00:00",
        user="alice", method="POST", path="/v1/messages",
        status=200, duration_ms=900.0, upstream_ms=850.0, level="INFO",
        prompt_tokens=100, completion_tokens=50, model="claude-sonnet-4-5",
        cache_read_tokens=80, cache_creation_tokens=10,
        service_name="anthropic-main", session_id="s1",
    )
    insert_log(
        timestamp="2026-08-01T12:00:00+00:00",
        user="bob", method="POST", path="/v1/messages",
        status=200, duration_ms=700.0, upstream_ms=650.0, level="INFO",
        prompt_tokens=200, completion_tokens=100, model="claude-sonnet-4-5",
        cache_read_tokens=50, cache_creation_tokens=0,
        service_name="anthropic-main", session_id="s2",
    )
    insert_log(
        timestamp="2026-08-02T09:30:00+00:00",
        user="alice", method="POST", path="/v1/chat/completions",
        status=200, duration_ms=300.0, upstream_ms=250.0, level="INFO",
        prompt_tokens=60, completion_tokens=20, model="gpt-5",
        cache_read_tokens=0, cache_creation_tokens=0,
        service_name="openai-main", session_id="s3",
    )
    insert_log(
        timestamp="2026-08-02T10:00:00+00:00",
        user="alice", method="POST", path="/v1/chat/completions",
        status=502, duration_ms=5000.0, upstream_ms=None, level="ERROR",
        session_id="s3",
    )


class TestStatsAPI:

    def test_requires_auth(self, client):
        resp = client.get("/api/stats/tokens")
        assert resp.status_code == 401

    def test_tokens_by_day(self, client):
        _seed_usage()
        resp = client.get("/api/stats/tokens", headers=AUTH)
        assert resp.status_code == 200
        rows = resp.json()["items"]
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-08-01"]["requests"] == 2
        assert by_date["2026-08-01"]["prompt_tokens"] == 300
        assert by_date["2026-08-01"]["completion_tokens"] == 150
        assert by_date["2026-08-02"]["requests"] == 2
        assert by_date["2026-08-02"]["prompt_tokens"] == 60

    def test_tokens_by_day_includes_cache_columns(self, client):
        """Cache tokens must be visible so Total tokens can use the full
        provider-reported volume (prompt + completion + cache r/w)."""
        _seed_usage()
        resp = client.get("/api/stats/tokens", headers=AUTH)
        rows = resp.json()["items"]
        by_date = {r["date"]: r for r in rows}
        assert by_date["2026-08-01"]["cache_read_tokens"] == 130
        assert by_date["2026-08-01"]["cache_creation_tokens"] == 10
        assert by_date["2026-08-02"]["cache_read_tokens"] == 0
        assert by_date["2026-08-02"]["cache_creation_tokens"] == 0

    def test_tokens_grouped_includes_cache_columns(self, client):
        _seed_usage()
        resp = client.get("/api/stats/tokens?group_by=model", headers=AUTH)
        rows = resp.json()["items"]
        sonnet = next(r for r in rows if r["model"] == "claude-sonnet-4-5")
        assert sonnet["cache_read_tokens"] == 130
        assert sonnet["cache_creation_tokens"] == 10

    def test_tokens_grouped_ranked_by_total_including_cache(self, client):
        """Leaderboard order must follow the displayed total (all four
        token components), not the old prompt+completion-only sum."""
        _seed_usage()
        insert_log(
            timestamp="2026-08-01T11:00:00+00:00",
            user="charlie", method="POST", path="/v1/messages",
            status=200, duration_ms=400.0, upstream_ms=380.0, level="INFO",
            prompt_tokens=10, completion_tokens=10, model="claude-sonnet-4-5",
            cache_read_tokens=10000, cache_creation_tokens=0,
            service_name="anthropic-main",
        )
        resp = client.get("/api/stats/tokens?group_by=user", headers=AUTH)
        rows = resp.json()["items"]
        assert rows[0]["user"] == "charlie"  # 10020 total, but only 20 p+c

    def test_top_models_total_tokens_includes_cache(self, client):
        """total_tokens = prompt + completion + cache_read + cache_creation,
        matching what providers report as actual usage."""
        _seed_usage()
        resp = client.get("/api/stats/models?limit=10", headers=AUTH)
        rows = resp.json()["items"]
        assert rows[0]["model"] == "claude-sonnet-4-5"
        # 300 prompt + 150 completion + 130 cache_read + 10 cache_creation
        assert rows[0]["total_tokens"] == 590

    def test_tokens_grouped_by_user(self, client):
        _seed_usage()
        resp = client.get("/api/stats/tokens?group_by=user", headers=AUTH)
        rows = resp.json()["items"]
        alice = next(r for r in rows if r["user"] == "alice")
        assert alice["requests"] == 3
        assert alice["prompt_tokens"] == 160
        assert alice["completion_tokens"] == 70

    def test_tokens_grouped_by_model(self, client):
        _seed_usage()
        resp = client.get("/api/stats/tokens?group_by=model", headers=AUTH)
        rows = resp.json()["items"]
        sonnet = next(r for r in rows if r["model"] == "claude-sonnet-4-5")
        assert sonnet["requests"] == 2
        assert sonnet["completion_tokens"] == 150

    def test_tokens_user_filter(self, client):
        _seed_usage()
        resp = client.get("/api/stats/tokens?group_by=user&user=bob", headers=AUTH)
        rows = resp.json()["items"]
        assert len(rows) == 1
        assert rows[0]["user"] == "bob"

    def test_cache_by_day(self, client):
        _seed_usage()
        resp = client.get("/api/stats/cache", headers=AUTH)
        rows = resp.json()["items"]
        day1 = next(r for r in rows if r["date"] == "2026-08-01")
        assert day1["cache_read_tokens"] == 130
        assert day1["cache_creation_tokens"] == 10
        # hit ratio = cache_read / (prompt + cache_read + cache_creation)
        assert day1["hit_ratio"] == pytest.approx(130 / (300 + 130 + 10), rel=1e-6)

    def test_requests_by_day(self, client):
        _seed_usage()
        resp = client.get("/api/stats/requests", headers=AUTH)
        rows = resp.json()["items"]
        day2 = next(r for r in rows if r["date"] == "2026-08-02")
        assert day2["requests"] == 2
        assert day2["errors"] == 1
        assert day2["avg_duration_ms"] == pytest.approx((300.0 + 5000.0) / 2, rel=1e-6)

    def test_top_models(self, client):
        _seed_usage()
        resp = client.get("/api/stats/models?limit=10", headers=AUTH)
        rows = resp.json()["items"]
        assert rows[0]["model"] == "claude-sonnet-4-5"
        # prompt 300 + completion 150 + cache_read 130 + cache_creation 10
        assert rows[0]["total_tokens"] == 590
        assert rows[0]["requests"] == 2
        names = [r["model"] for r in rows]
        assert "gpt-5" in names

    def test_date_range_filter(self, client):
        _seed_usage()
        resp = client.get(
            "/api/stats/tokens?start_date=2026-08-02&end_date=2026-08-02",
            headers=AUTH,
        )
        rows = resp.json()["items"]
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-08-02"

    def test_report_page_served(self, client):
        resp = client.get("/report")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Usage Report" in resp.text


# ---------------------------------------------------------------------------
# Cost estimation (P1-4)
# ---------------------------------------------------------------------------

MODEL_PRICES_FIXTURE = {
    "claude-sonnet-4-5": {
        "prompt": 3.0, "completion": 15.0,
        "cache_read": 0.3, "cache_creation": 3.0,
    },
    "gpt-5": {"prompt": 1.25, "completion": 10.0, "cache_read": 0.125},
}


class TestCostEstimation:

    def test_cost_by_day(self, client, monkeypatch):
        monkeypatch.setattr(config, "MODEL_PRICES", MODEL_PRICES_FIXTURE)
        _seed_usage()
        resp = client.get("/api/stats/cost", headers=AUTH)
        assert resp.status_code == 200
        rows = resp.json()["items"]
        by_date = {r["date"]: r for r in rows}
        # 08-01: sonnet alice (100p/50c/80cr/10cc) + bob (200p/100c/50cr)
        day1 = (
            100 * 3.0 + 50 * 15.0 + 80 * 0.3 + 10 * 3.0
            + 200 * 3.0 + 100 * 15.0 + 50 * 0.3
        ) / 1_000_000
        assert by_date["2026-08-01"]["cost"] == pytest.approx(day1, rel=1e-6)
        # 08-02: gpt-5 alice (60p/20c); the 502 row has no tokens/model
        day2 = (60 * 1.25 + 20 * 10.0) / 1_000_000
        assert by_date["2026-08-02"]["cost"] == pytest.approx(day2, rel=1e-6)

    def test_cost_grouped_by_user(self, client, monkeypatch):
        monkeypatch.setattr(config, "MODEL_PRICES", MODEL_PRICES_FIXTURE)
        _seed_usage()
        resp = client.get("/api/stats/cost?group_by=user", headers=AUTH)
        assert resp.status_code == 200
        rows = resp.json()["items"]
        assert rows[0]["user"] == "bob"  # highest cost first
        bob = (
            200 * 3.0 + 100 * 15.0 + 50 * 0.3
        ) / 1_000_000
        assert rows[0]["cost"] == pytest.approx(bob, rel=1e-6)
        alice = next(r for r in rows if r["user"] == "alice")
        alice_cost = (
            100 * 3.0 + 50 * 15.0 + 80 * 0.3 + 10 * 3.0
            + 60 * 1.25 + 20 * 10.0
        ) / 1_000_000
        assert alice["cost"] == pytest.approx(alice_cost, rel=1e-6)

    def test_cost_grouped_by_model(self, client, monkeypatch):
        monkeypatch.setattr(config, "MODEL_PRICES", MODEL_PRICES_FIXTURE)
        _seed_usage()
        resp = client.get("/api/stats/cost?group_by=model", headers=AUTH)
        rows = resp.json()["items"]
        by_model = {r["model"]: r for r in rows}
        sonnet_cost = (
            300 * 3.0 + 150 * 15.0 + 130 * 0.3 + 10 * 3.0
        ) / 1_000_000
        assert by_model["claude-sonnet-4-5"]["cost"] == pytest.approx(sonnet_cost, rel=1e-6)

    def test_cost_unknown_model_is_zero(self, client, monkeypatch):
        """Models without a configured price contribute zero cost."""
        monkeypatch.setattr(config, "MODEL_PRICES", {})
        _seed_usage()
        resp = client.get("/api/stats/cost", headers=AUTH)
        rows = resp.json()["items"]
        assert all(r["cost"] == 0.0 for r in rows)

    def test_cost_requires_auth(self, client):
        resp = client.get("/api/stats/cost")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DB-managed model prices (override env, feed cost reports)
# ---------------------------------------------------------------------------

class TestModelPricesAPI:

    def test_prices_require_auth(self, client):
        assert client.get("/api/prices").status_code == 401
        assert client.post("/api/prices", json={"model": "x"}).status_code == 401

    def test_price_crud_cycle(self, client):
        resp = client.post("/api/prices", headers=AUTH, json={
            "model": "qwen3.8-flash",
            "prompt": 0.2, "completion": 0.8,
            "cache_read": 0.05, "cache_creation": 0.2,
        })
        assert resp.status_code == 200
        assert resp.json()["model"] == "qwen3.8-flash"

        items = client.get("/api/prices", headers=AUTH).json()["items"]
        assert [r["model"] for r in items] == ["qwen3.8-flash"]

        # Upsert updates in place (no duplicate rows).
        client.post("/api/prices", headers=AUTH, json={"model": "qwen3.8-flash", "prompt": 0.3})
        items = client.get("/api/prices", headers=AUTH).json()["items"]
        assert len(items) == 1
        assert items[0]["prompt"] == 0.3
        assert items[0]["completion"] == 0.0  # omitted fields reset to 0

        resp = client.delete("/api/prices/qwen3.8-flash", headers=AUTH)
        assert resp.status_code == 200
        assert client.delete("/api/prices/qwen3.8-flash", headers=AUTH).status_code == 404

    def test_negative_price_rejected(self, client):
        resp = client.post("/api/prices", headers=AUTH, json={"model": "m", "prompt": -1})
        assert resp.status_code == 400

    def test_catalog_lists_defaults_with_configured_flag(self, client):
        client.post("/api/prices", headers=AUTH, json={"model": "gpt-4o", "prompt": 2.5})
        items = client.get("/api/prices/catalog", headers=AUTH).json()["items"]
        gpt4o = next(i for i in items if i["model"] == "gpt-4o")
        other = next(i for i in items if i["model"] == "gpt-4o-mini")
        assert gpt4o["configured"] is True
        assert other["configured"] is False

    def test_db_price_flows_into_cost_report(self, client, monkeypatch):
        """Admin-edited prices apply without env config or restart."""
        monkeypatch.setattr(config, "MODEL_PRICES", {})
        _seed_usage()
        client.post("/api/prices", headers=AUTH, json={
            "model": "claude-sonnet-4-5",
            "prompt": 3.0, "completion": 15.0, "cache_read": 0.3, "cache_creation": 3.0,
        })
        resp = client.get("/api/stats/cost?group_by=model", headers=AUTH)
        by_model = {r["model"]: r for r in resp.json()["items"]}
        sonnet_cost = (300 * 3.0 + 150 * 15.0 + 130 * 0.3 + 10 * 3.0) / 1_000_000
        assert by_model["claude-sonnet-4-5"]["cost"] == pytest.approx(sonnet_cost, rel=1e-6)
        # Unpriced models still contribute zero.
        assert by_model["gpt-5"]["cost"] == 0.0

    def test_db_price_overrides_env(self, client, monkeypatch):
        monkeypatch.setattr(config, "MODEL_PRICES", {"gpt-5": {"prompt": 1.25}})
        client.post("/api/prices", headers=AUTH, json={"model": "gpt-5", "prompt": 9.9})
        from gateway.prices import effective_prices
        assert effective_prices()["gpt-5"]["prompt"] == 9.9


# ---------------------------------------------------------------------------
# Latency percentiles and error breakdown
# ---------------------------------------------------------------------------

class TestLatencyPercentiles:

    def test_request_stats_include_percentiles(self, client):
        _seed_usage()  # 08-01: 900/700, 08-02: 300/5000
        resp = client.get("/api/stats/requests", headers=AUTH)
        rows = resp.json()["items"]
        day1 = next(r for r in rows if r["date"] == "2026-08-01")
        day2 = next(r for r in rows if r["date"] == "2026-08-02")
        # p50 with 2 rows is the lower of the pair (rank ceil(2*.5)=1).
        assert day1["p50"] == 700.0
        assert day1["p95"] == 900.0
        assert day1["p99"] == 900.0
        assert day2["p50"] == 300.0
        assert day2["p95"] == 5000.0

    def test_percentile_distribution_ordering(self, client):
        for i in range(1, 101):
            insert_log(
                timestamp=f"2026-08-03T12:00:00+00:00",
                user="alice", method="GET", path="/v1/models",
                status=200, duration_ms=float(i), upstream_ms=None, level="INFO",
            )
        resp = client.get("/api/stats/requests", headers=AUTH)
        row = next(r for r in resp.json()["items"] if r["date"] == "2026-08-03")
        assert row["p50"] == 50.0
        assert row["p95"] == 95.0
        assert row["p99"] == 99.0

    def test_latency_summary_endpoint(self, client):
        _seed_usage()
        resp = client.get("/api/stats/latency", headers=AUTH)
        assert resp.status_code == 200
        data = resp.json()
        assert data["requests"] == 4
        assert data["avg_duration_ms"] == pytest.approx((900 + 700 + 300 + 5000) / 4, rel=1e-6)
        assert data["p50"] == 700.0
        assert data["p95"] == 5000.0
        assert data["max_duration_ms"] == 5000.0

    def test_latency_summary_empty_range(self, client):
        resp = client.get(
            "/api/stats/latency?start_date=2030-01-01", headers=AUTH,
        )
        data = resp.json()
        assert data["requests"] == 0
        assert data["p95"] == 0.0

    def test_latency_requires_auth(self, client):
        assert client.get("/api/stats/latency").status_code == 401

    def test_date_filter_applies(self, client):
        _seed_usage()
        resp = client.get(
            "/api/stats/latency?start_date=2026-08-02&end_date=2026-08-02",
            headers=AUTH,
        )
        data = resp.json()
        assert data["requests"] == 2
        assert data["p50"] == 300.0


class TestErrorsBreakdown:

    def test_errors_by_status(self, client):
        _seed_usage()  # one 502
        insert_log(
            timestamp="2026-08-02T11:00:00+00:00",
            user="bob", method="POST", path="/v1/messages",
            status=429, duration_ms=10.0, upstream_ms=None, level="WARN",
        )
        resp = client.get("/api/stats/errors", headers=AUTH)
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert {i["key"]: i["count"] for i in items} == {502: 1, 429: 1}

    def test_errors_by_model(self, client):
        _seed_usage()  # 502 row has no model
        resp = client.get("/api/stats/errors?group_by=model", headers=AUTH)
        items = resp.json()["items"]
        assert items == [{"key": "(unknown)", "count": 1}]

    def test_invalid_group_by_rejected(self, client):
        resp = client.get("/api/stats/errors?group_by=password", headers=AUTH)
        assert resp.status_code == 422

    def test_errors_requires_auth(self, client):
        assert client.get("/api/stats/errors").status_code == 401


# ---------------------------------------------------------------------------
# End-to-end: streamed SSE usage reaches the audit log and /api/stats
# ---------------------------------------------------------------------------

class _FakeSseResponse:
    """Minimal stand-in for an httpx streaming response."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.headers = {"content-type": "text/event-stream"}
        self.status_code = 200

    def aiter_bytes(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()

    async def aclose(self):
        pass


class _FakeSseClient:
    def __init__(self, chunks):
        self._chunks = chunks

    def build_request(self, **kwargs):
        return kwargs

    async def send(self, request, stream=False):
        return _FakeSseResponse(self._chunks)

    async def aclose(self):
        pass


class TestStreamedAuditLogEndToEnd:
    """Regression: SSE usage must land in audit_logs despite deferred drain."""

    SSE_CHUNKS = [
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":25,"cache_creation_input_tokens":1500,'
        b'"cache_read_input_tokens":7123}}}\n\n',
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n',
        b'event: message_delta\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":321}}\n\n',
    ]

    def _client(self, monkeypatch):
        import gateway.config as config
        import gateway.main as gm

        keys_dir = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "auth-server", "auth_server", "keys",
        )
        os.makedirs(keys_dir, exist_ok=True)
        import sys
        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "..", "auth-server"),
        )
        from auth_server.jwt_utils import _ensure_keys_exist
        _ensure_keys_exist()
        config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
        config.AUDIT_API_TOKEN = "test-audit-token"

        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="stats_sse_test_")
        os.close(fd)
        old_db = config.AUDIT_DB_PATH
        config.AUDIT_DB_PATH = tmp
        init_audit_db()
        monkeypatch.setattr(gm, "init_audit_db", lambda: None)

        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        import time as _time
        now = int(_time.time())
        token = pyjwt.encode(
            {"sub": "alice", "username": "alice", "jti": "j1",
             "iat": now, "exp": now + 3600},
            key, algorithm="RS256",
        )
        pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
        key_tmp.write(pub)
        key_tmp.close()
        gm._public_key = None
        config.JWT_PUBLIC_KEY_PATH = key_tmp.name

        async def fake_routing(payload):
            return {
                "service_name": "anthropic-main",
                "upstream_url": "https://api.example.com",
                "upstream_api_key": "k",
                "upstream_auth_type": "x-api-key",
                "models": [],
            }, None

        async def fake_revoked(jti):
            return False

        monkeypatch.setattr(gm, "_resolve_user_routing", fake_routing)
        monkeypatch.setattr(gm, "_is_jti_revoked", fake_revoked)
        config.UPSTREAM_URL_ALLOWLIST = {"api.example.com"}
        monkeypatch.setattr(
            gm.httpx, "AsyncClient", lambda **kw: _FakeSseClient(self.SSE_CHUNKS),
        )

        client = TestClient(gm.app)
        old_allowlist = config.UPSTREAM_URL_ALLOWLIST
        yield client, token, tmp, key_tmp.name, old_db
        config.AUDIT_DB_PATH = old_db
        config.UPSTREAM_URL_ALLOWLIST = old_allowlist
        for path in (tmp, key_tmp.name):
            if os.path.exists(path):
                os.remove(path)

    def test_sse_usage_persisted_to_audit_log(self, monkeypatch):
        gen = self._client(monkeypatch)
        client, token, _tmp, _key, _old = next(gen)
        try:
            resp = client.post(
                "/v1/messages",
                json={"model": "deepseek-v4-pro", "stream": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            # Drain complete: the deferred audit task has been flushed by
            # the time the client finishes consuming the stream.
            rows = query_logs({"page": 1, "page_size": 10})["items"]
            assert len(rows) == 1
            row = rows[0]
            assert row["prompt_tokens"] == 25
            assert row["completion_tokens"] == 321
            assert row["cache_read_tokens"] == 7123
            assert row["cache_creation_tokens"] == 1500
            assert row["upstream_ms"] is not None

            days = cache_usage()
            assert len(days) == 1
            assert days[0]["cache_read_tokens"] == 7123
            assert days[0]["hit_ratio"] == pytest.approx(
                7123 / (25 + 7123 + 1500), rel=1e-6,
            )
        finally:
            try:
                next(gen)
            except StopIteration:
                pass
