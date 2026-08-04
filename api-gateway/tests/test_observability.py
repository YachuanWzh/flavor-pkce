"""Test structured logging and Prometheus metrics for the API Gateway."""

import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.logging import log_request


# ---------------------------------------------------------------------------
# path-normalization helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("/health", "/health"),
        ("/v1/models", "/v1/models"),
        ("/v1/chat/completions", "/v1/chat"),
        ("/v1/chat/completions/extra", "/v1/chat"),
        ("/", "/"),
        ("", "/"),
    ],
)
def test_normalize_path(raw, expected):
    from gateway.metrics import normalize_path
    assert normalize_path(raw) == expected


# ---------------------------------------------------------------------------
# logging — unit tests
# ---------------------------------------------------------------------------

class _CaptureHandler(logging.Handler):
    """Handler that stores formatted log records in a list."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class TestJsonLogging:
    """Verify log_request outputs valid JSON lines."""

    @pytest.fixture(autouse=True)
    def setup(self):
        logger = logging.getLogger("gateway")
        logger.handlers.clear()
        self._handler = _CaptureHandler()
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(self._handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        yield
        logger.removeHandler(self._handler)

    def test_log_request_produces_valid_json(self):
        log_request(
            user="test-user-123",
            method="POST",
            path="/v1/chat/completions",
            status=200,
            duration_ms=1234.56,
            upstream_ms=1100.12,
        )

        assert len(self._handler.lines) == 1
        entry = json.loads(self._handler.lines[0])

        assert entry["user"] == "test-user-123"
        assert entry["method"] == "POST"
        assert entry["path"] == "/v1/chat/completions"
        assert entry["status"] == 200
        assert entry["duration_ms"] == 1234.56
        assert entry["upstream_ms"] == 1100.12
        assert entry["level"] == "INFO"
        assert "timestamp" in entry

    def test_log_request_anon_user(self):
        log_request(method="GET", path="/health", status=200, duration_ms=1.0)
        entry = json.loads(self._handler.lines[0])
        assert entry["user"] == "-"
        assert "upstream_ms" not in entry  # omitted when None

    def test_log_request_error_level(self):
        log_request(
            method="POST",
            path="/v1/chat/completions",
            status=502,
            duration_ms=5000.0,
            level="ERROR",
        )
        entry = json.loads(self._handler.lines[0])
        assert entry["level"] == "ERROR"


# ---------------------------------------------------------------------------
# /metrics endpoint — integration tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def setup_keys():
    """Re-use auth-server keys so JWT verification works."""
    import os
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


# Helper: create a signed JWT
def _create_jwt(sub="test-user"):
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    payload = {
        "sub": sub,
        "client_id": "test",
        "scope": "models:use",
        "iat": now,
        "exp": now + 3600,
        "jti": "test-jti",
    }
    token = pyjwt.encode(payload, key, algorithm="RS256")
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return token, pub_pem


def _set_gateway_key(pub_pem: bytes):
    import tempfile, os
    import gateway.main as gm

    tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    tmp.write(pub_pem)
    tmp.close()

    gm._public_key = None
    config.JWT_PUBLIC_KEY_PATH = tmp.name
    return tmp.name


class TestMetricsEndpoint:
    """Integration tests for the /metrics endpoint."""

    def test_metrics_returns_prometheus_format(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        body = resp.text
        assert "gateway_requests_total" in body
        assert "gateway_request_duration_seconds" in body
        assert "gateway_upstream_errors_total" in body
        assert "gateway_active_connections" in body

    def test_metrics_incremented_after_auth_failure(self, client):
        """401 responses should be counted in request_total."""
        before = client.get("/metrics").text

        # Trigger a 401
        client.get("/v1/models")

        after = client.get("/metrics").text
        assert after != before

    def test_metrics_incremented_after_successful_request(self, client):
        """200 responses should be counted."""
        token, pub = _create_jwt("alice")
        key_path = _set_gateway_key(pub)

        try:
            before = client.get("/metrics").text

            resp = client.get(
                "/get",
                headers={"Authorization": f"Bearer {token}"},
            )
            # JWT was accepted (should not be 401)
            assert resp.status_code != 401

            after = client.get("/metrics").text
            assert after != before
        finally:
            import os
            os.unlink(key_path)

    def test_active_connections_gauge_present(self, client):
        """The active_connections gauge is present and reports a value >= 0.

        We cannot assert exactly 0 because the /metrics request itself
        is in-flight when the gauge value is sampled.
        """
        import re
        resp = client.get("/metrics")
        m = re.search(r"gateway_active_connections\s+([\d.]+)", resp.text)
        assert m is not None, "gauge not found in metrics output"
        assert float(m.group(1)) >= 0.0

    def test_user_sub_logged_in_request(self, client):
        """The user sub should appear in logs after a successful request."""
        token, pub = _create_jwt("bob-the-builder")
        key_path = _set_gateway_key(pub)

        # Capture log output via a proper handler
        logger = logging.getLogger("gateway")
        capture = _CaptureHandler()
        capture.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(capture)

        try:
            client.get("/get", headers={"Authorization": f"Bearer {token}"})
        finally:
            import os
            os.unlink(key_path)
            logger.removeHandler(capture)

        # All log lines produced during the test
        output = "\n".join(capture.lines)
        assert "bob-the-builder" in output

        # Each line must be valid JSON
        for line in capture.lines:
            if not line.strip():
                continue
            entry = json.loads(line)
            assert "timestamp" in entry
            assert "user" in entry

    def test_upstream_error_counter(self, client):
        """When upstream is unreachable, upstream_errors_total should increment."""
        token, pub = _create_jwt()
        key_path = _set_gateway_key(pub)

        old_upstream = config.UPSTREAM_URL
        config.UPSTREAM_URL = "http://127.0.0.1:1"  # nothing listening here

        try:
            before = client.get("/metrics").text

            resp = client.get(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {token}"},
            )
            # 502 means our error handling kicked in
            assert resp.status_code == 502

            after = client.get("/metrics").text

            # upstream_errors_total should have increased
            import re
            before_match = re.search(
                r"gateway_upstream_errors_total\{[^}]*\}\s+([\d.]+)", before
            )
            after_match = re.search(
                r"gateway_upstream_errors_total\{[^}]*\}\s+([\d.]+)", after
            )
            before_val = float(before_match.group(1)) if before_match else 0.0
            after_val = float(after_match.group(1)) if after_match else 0.0
            assert after_val > before_val
        finally:
            config.UPSTREAM_URL = old_upstream
            import os
            os.unlink(key_path)
