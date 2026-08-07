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
        assert rows[0]["total_tokens"] == 450
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
