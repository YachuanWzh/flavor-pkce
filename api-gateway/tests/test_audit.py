"""Test audit-log database operations and the /api/logs endpoint."""

import json
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_log, query_logs


# ---------------------------------------------------------------------------
# Database unit tests
# ---------------------------------------------------------------------------

class TestAuditDatabase:
    """Direct tests for SQLite insert / query."""

    @pytest.fixture(autouse=True)
    def setup(self):
        fd, self._tmp = tempfile.mkstemp(suffix=".db", prefix="audit_test_")
        os.close(fd)
        self._old = config.AUDIT_DB_PATH
        config.AUDIT_DB_PATH = self._tmp
        init_audit_db()
        yield
        config.AUDIT_DB_PATH = self._old
        if os.path.exists(self._tmp):
            os.remove(self._tmp)

    def _insert_sample_logs(self):
        insert_log(
            timestamp="2026-07-20T10:00:00+00:00",
            user="alice", method="POST", path="/v1/chat/completions",
            status=200, duration_ms=1234.5, upstream_ms=1100.0, level="INFO",
        )
        insert_log(
            timestamp="2026-07-21T14:30:00+00:00",
            user="bob", method="GET", path="/v1/models",
            status=200, duration_ms=45.0, upstream_ms=None, level="INFO",
        )
        insert_log(
            timestamp="2026-07-21T18:00:00+00:00",
            user="bob", method="POST", path="/v1/chat/completions",
            status=502, duration_ms=5000.0, upstream_ms=None, level="ERROR",
        )

    def test_insert_and_query_all(self):
        self._insert_sample_logs()
        result = query_logs({"page": 1, "page_size": 10})
        assert result["total"] == 3
        assert len(result["items"]) == 3
        # Most recent first
        assert result["items"][0]["user"] == "bob"
        assert result["items"][0]["status"] == 502

    def test_pagination(self):
        for i in range(25):
            insert_log(
                timestamp=f"2026-07-20T00:{i:02d}:00+00:00",
                user="test", method="GET", path="/v1/models",
                status=200, duration_ms=10.0, upstream_ms=None, level="INFO",
            )

        page1 = query_logs({"page": 1, "page_size": 10})
        assert page1["total"] == 25
        assert len(page1["items"]) == 10
        assert page1["page"] == 1

        page3 = query_logs({"page": 3, "page_size": 10})
        assert len(page3["items"]) == 5  # last page

    def test_date_filter_start(self):
        self._insert_sample_logs()
        result = query_logs({"start_date": "2026-07-21", "page": 1, "page_size": 10})
        assert result["total"] == 2

    def test_date_filter_end(self):
        self._insert_sample_logs()
        result = query_logs({"end_date": "2026-07-20", "page": 1, "page_size": 10})
        assert result["total"] == 1

    def test_date_filter_range(self):
        self._insert_sample_logs()
        result = query_logs({"start_date": "2026-07-21", "end_date": "2026-07-21", "page": 1, "page_size": 10})
        assert result["total"] == 2

    def test_keyword_filter(self):
        self._insert_sample_logs()
        result = query_logs({"keyword": "chat", "page": 1, "page_size": 10})
        assert result["total"] == 2

    def test_keyword_filter_user_field(self):
        self._insert_sample_logs()
        result = query_logs({"keyword": "alice", "page": 1, "page_size": 10})
        assert result["total"] == 1
        assert result["items"][0]["user"] == "alice"

    def test_user_exact_filter(self):
        self._insert_sample_logs()
        result = query_logs({"user": "bob", "page": 1, "page_size": 10})
        assert result["total"] == 2

    def test_combined_filters(self):
        self._insert_sample_logs()
        result = query_logs({
            "start_date": "2026-07-21",
            "end_date": "2026-07-21",
            "user": "bob",
            "keyword": "models",
            "page": 1,
            "page_size": 10,
        })
        assert result["total"] == 1
        assert result["items"][0]["path"] == "/v1/models"

    def test_empty_result(self):
        self._insert_sample_logs()
        result = query_logs({"keyword": "nonexistent", "page": 1, "page_size": 10})
        assert result["total"] == 0
        assert result["items"] == []

    def test_model_column_persisted(self):
        """model should be stored and returned."""
        insert_log(
            timestamp="2026-07-22T12:00:00+00:00",
            user="alice", method="POST", path="/v1/chat/completions",
            status=200, duration_ms=500.0, upstream_ms=450.0, level="INFO",
            prompt_tokens=150, completion_tokens=80, model="deepseek-v4-pro",
        )
        result = query_logs({"page": 1, "page_size": 1})
        item = result["items"][0]
        assert item["model"] == "deepseek-v4-pro"

    def test_model_column_null_by_default(self):
        """When model is not provided, the column should be NULL."""
        insert_log(
            timestamp="2026-07-22T12:00:00+00:00",
            user="bob", method="GET", path="/v1/models",
            status=200, duration_ms=10.0, upstream_ms=None, level="INFO",
        )
        result = query_logs({"page": 1, "page_size": 1})
        item = result["items"][0]
        assert item["model"] is None

    def test_keyword_searches_model(self):
        """Keyword filter should match on the model column."""
        insert_log(
            timestamp="2026-07-22T12:00:00+00:00",
            user="alice", method="POST", path="/v1/chat/completions",
            status=200, duration_ms=500.0, upstream_ms=450.0, level="INFO",
            model="claude-sonnet-4-5",
        )
        result = query_logs({"keyword": "claude", "page": 1, "page_size": 10})
        assert result["total"] == 1

    def test_token_columns_persisted(self):
        """prompt_tokens and completion_tokens should be stored and returned."""
        insert_log(
            timestamp="2026-07-22T12:00:00+00:00",
            user="alice", method="POST", path="/v1/chat/completions",
            status=200, duration_ms=500.0, upstream_ms=450.0, level="INFO",
            prompt_tokens=150, completion_tokens=80,
        )
        result = query_logs({"page": 1, "page_size": 1})
        item = result["items"][0]
        assert item["prompt_tokens"] == 150
        assert item["completion_tokens"] == 80

    def test_token_columns_null_by_default(self):
        """When tokens are not provided, columns should be NULL (None in Python)."""
        insert_log(
            timestamp="2026-07-22T12:00:00+00:00",
            user="bob", method="GET", path="/v1/models",
            status=200, duration_ms=10.0, upstream_ms=None, level="INFO",
        )
        result = query_logs({"page": 1, "page_size": 1})
        item = result["items"][0]
        assert item["prompt_tokens"] is None
        assert item["completion_tokens"] is None


# ---------------------------------------------------------------------------
# API endpoint integration tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="audit_api_test_")
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
    """Re-use auth-server keys so JWT verification works."""
    keys_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-server", "auth_server", "keys"
    )
    if not os.path.exists(keys_dir):
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


class _AuthedClient(TestClient):
    """TestClient that sends the audit token on /api/logs calls."""


class TestAuditAPI:
    """Integration tests for GET /api/logs and GET /audit."""

    def _headers(self):
        return {"X-Audit-Token": "test-audit-token"}

    def test_api_logs_empty(self, client):
        resp = client.get("/api/logs", headers=self._headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_api_logs_after_request(self, client):
        """After a proxied request, a log entry should appear in the API."""
        # Make a request that gets logged (401 counts)
        client.get("/v1/models")
        # Make another request that we can see in the logs
        client.get("/v1/chat/completions")

        resp = client.get("/api/logs", headers=self._headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 2

    def test_api_logs_pagination(self, client):
        """Page size parameter should be respected."""
        # Generate several requests to fill logs
        for _ in range(5):
            client.get("/v1/models")

        resp = client.get("/api/logs?page_size=2", headers=self._headers())
        data = resp.json()
        assert len(data["items"]) <= 2
        assert data["page_size"] == 2

    def test_api_logs_keyword_filter(self, client):
        """Keyword filter should work via API."""
        client.get("/v1/chat/completions")
        client.get("/v1/models")

        resp = client.get("/api/logs?keyword=chat", headers=self._headers())
        data = resp.json()
        for item in data["items"]:
            has_match = ("chat" in item["path"].lower()) or ("chat" in item["user"].lower())
            assert has_match

    def test_api_logs_date_filter(self, client):
        """Date range filter should work via API."""
        client.get("/v1/models")

        # Future date — no results
        resp = client.get("/api/logs?start_date=2099-01-01", headers=self._headers())
        data = resp.json()
        assert data["total"] == 0

    def test_api_logs_user_filter(self, client):
        """User filter should work via API."""
        client.get("/v1/models")  # unauthenticated, user="-"

        resp = client.get("/api/logs?user=-", headers=self._headers())
        data = resp.json()
        assert data["total"] >= 1

    def test_audit_page_served(self, client):
        """GET /audit should return the HTML viewer page."""
        resp = client.get("/audit")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
        assert "Gateway Audit Logs" in resp.text

    def test_logs_not_auto_logged(self, client):
        """Requests to /api/logs and /metrics should not be persisted to DB."""
        # Make a few self-referencing calls
        client.get("/api/logs", headers=self._headers())
        client.get("/metrics")

        resp = client.get("/api/logs", headers=self._headers())
        data = resp.json()
        # None of the logged items should have path /api/logs or /metrics
        for item in data["items"]:
            assert item["path"] not in ("/api/logs", "/metrics")

    def test_clear_logs_deletes_all(self, client):
        """DELETE /api/logs should remove all entries and return count."""
        # Insert some log entries first
        for _ in range(3):
            client.get("/v1/models")

        # Verify they exist
        before = client.get("/api/logs", headers=self._headers()).json()
        assert before["total"] >= 3

        # Clear
        resp = client.delete("/api/logs", headers=self._headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] >= 3

        # Verify empty
        after = client.get("/api/logs", headers=self._headers()).json()
        assert after["total"] == 0

    def test_token_columns_in_api_response(self, client):
        """The /api/logs response should include token and model fields."""
        client.get("/v1/models")

        resp = client.get("/api/logs", headers=self._headers())
        data = resp.json()
        assert len(data["items"]) > 0
        item = data["items"][0]
        assert "prompt_tokens" in item
        assert "completion_tokens" in item
        assert "model" in item

    def test_model_in_api_response_after_post(self, client):
        """A POST with a model field should be captured in the audit log."""
        time = __import__("time")
        pyjwt = __import__("jwt")
        rsa = __import__("cryptography.hazmat.primitives.asymmetric.rsa", fromlist=["rsa"])
        serialization = __import__("cryptography.hazmat.primitives.serialization", fromlist=["serialization"])

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        payload = {
            "sub": "u1", "username": "alice", "client_id": "test",
            "scope": "models:use", "iat": now, "exp": now + 3600, "jti": "j1",
        }
        token = pyjwt.encode(payload, key, algorithm="RS256")
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        import tempfile as _tf, os as _os
        tmp = _tf.NamedTemporaryFile(suffix=".pem", delete=False)
        tmp.write(pub_pem)
        tmp.close()

        import gateway.main as gm
        old_key = gm._public_key
        old_path = config.JWT_PUBLIC_KEY_PATH
        gm._public_key = None
        config.JWT_PUBLIC_KEY_PATH = tmp.name

        try:
            # POST with model in body so the gateway extracts it
            client.post(
                "/v1/chat/completions",
                json={"model": "deepseek-v4-pro", "messages": []},
                headers={"Authorization": f"Bearer {token}"},
            )

            resp = client.get("/api/logs", headers=self._headers())
            data = resp.json()
            models = {item.get("model") for item in data["items"]}
            assert "deepseek-v4-pro" in models
        finally:
            gm._public_key = old_key
            config.JWT_PUBLIC_KEY_PATH = old_path
            _os.unlink(tmp.name)

    def test_username_extracted_from_jwt(self, client):
        """When JWT has a 'username' claim, it should be used for logging."""
        time = __import__("time")
        pyjwt = __import__("jwt")
        rsa = __import__("cryptography.hazmat.primitives.asymmetric.rsa", fromlist=["rsa"])
        serialization = __import__("cryptography.hazmat.primitives.serialization", fromlist=["serialization"])

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        payload = {
            "sub": "uuid-12345",
            "username": "real-human-name",
            "client_id": "test",
            "scope": "models:use",
            "iat": now,
            "exp": now + 3600,
            "jti": "test-jti",
        }
        token_str = pyjwt.encode(payload, key, algorithm="RS256")
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

        import tempfile as _tf, os as _os
        tmp = _tf.NamedTemporaryFile(suffix=".pem", delete=False)
        tmp.write(pub_pem)
        tmp.close()

        import gateway.main as gm
        old_key = gm._public_key
        old_path = config.JWT_PUBLIC_KEY_PATH
        gm._public_key = None
        config.JWT_PUBLIC_KEY_PATH = tmp.name

        try:
            client.get("/v1/chat/completions", headers={
                "Authorization": f"Bearer {token_str}",
            })

            resp = client.get("/api/logs", headers=self._headers())
            data = resp.json()
            usernames = {item["user"] for item in data["items"]}
            assert "real-human-name" in usernames
        finally:
            gm._public_key = old_key
            config.JWT_PUBLIC_KEY_PATH = old_path
            _os.unlink(tmp.name)


# ---------------------------------------------------------------------------
# Token usage extraction unit tests
# ---------------------------------------------------------------------------

class TestTokenExtraction:
    """Direct tests for _extract_full_token_usage and SSE chunk parsing."""

    def test_openai_format(self):
        from gateway.main import _extract_full_token_usage
        from unittest.mock import Mock

        body = b'{"usage":{"prompt_tokens":100,"completion_tokens":50,"total_tokens":150}}'
        req = Mock()
        _extract_full_token_usage(req, body)
        assert req.state.prompt_tokens == 100
        assert req.state.completion_tokens == 50

    def test_anthropic_format(self):
        from gateway.main import _extract_full_token_usage
        from unittest.mock import Mock

        body = b'{"usage":{"input_tokens":200,"output_tokens":80}}'
        req = Mock()
        _extract_full_token_usage(req, body)
        assert req.state.prompt_tokens == 200
        assert req.state.completion_tokens == 80

    def test_no_usage_field(self):
        from gateway.main import _extract_full_token_usage
        from unittest.mock import Mock

        body = b'{"choices":[{"finish_reason":"stop"}]}'
        req = Mock()
        _extract_full_token_usage(req, body)
        assert req.state.prompt_tokens is None
        assert req.state.completion_tokens is None

    def test_invalid_json(self):
        from gateway.main import _extract_full_token_usage
        from unittest.mock import Mock

        body = b'not json at all'
        req = Mock()
        _extract_full_token_usage(req, body)
        assert req.state.prompt_tokens is None
        assert req.state.completion_tokens is None

    def test_anthropic_message_start_sse(self):
        """Extract input_tokens from Anthropic message_start SSE event."""
        from gateway.main import _extract_input_tokens_from_chunk

        # Simulate a complete message_start SSE frame
        chunk = (
            b'event: message_start\r\n'
            b'data: {"type":"message_start","message":{'
            b'"id":"msg_001","type":"message","role":"assistant",'
            b'"usage":{"input_tokens":150}}}\r\n\r\n'
        )
        pt = _extract_input_tokens_from_chunk(chunk)
        assert pt == 150

    def test_openai_sse_no_tokens_in_first_chunk(self):
        """OpenAI SSE first chunk does not contain usage."""
        from gateway.main import _extract_input_tokens_from_chunk

        chunk = (
            b'data: {"id":"chatcmpl-001","object":"chat.completion.chunk",'
            b'"choices":[{"delta":{"role":"assistant"},"index":0}]}\r\n\r\n'
        )
        pt = _extract_input_tokens_from_chunk(chunk)
        assert pt is None

    def test_invalid_sse_chunk(self):
        """Malformed or non-SSE chunk should return None."""
        from gateway.main import _extract_input_tokens_from_chunk
        assert _extract_input_tokens_from_chunk(b'garbage data') is None


class TestModelExtraction:
    """Direct tests for _extract_model."""

    def test_openai_format(self):
        from gateway.main import _extract_model
        assert _extract_model(b'{"model":"gpt-5","messages":[]}') == "gpt-5"

    def test_anthropic_format(self):
        from gateway.main import _extract_model
        assert _extract_model(
            b'{"model":"claude-sonnet-4-5","max_tokens":4096}'
        ) == "claude-sonnet-4-5"

    def test_no_model_field(self):
        from gateway.main import _extract_model
        assert _extract_model(b'{"messages":[{"role":"user"}]}') is None

    def test_invalid_json(self):
        from gateway.main import _extract_model
        assert _extract_model(b'not json') is None

    def test_empty_body(self):
        from gateway.main import _extract_model
        assert _extract_model(b'') is None
