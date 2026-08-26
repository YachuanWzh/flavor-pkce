"""API tests for the read-only SQL endpoint /api/query."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_log


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="query_api_test_")
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


def test_requires_auth(client):
    resp = client.post("/api/query", json={"sql": "SELECT 1"})
    assert resp.status_code == 401


def test_select_ok(client):
    resp = client.post(
        "/api/query",
        headers=AUTH,
        json={"sql": "SELECT \"user\", COUNT(*) AS n FROM audit_logs GROUP BY \"user\""},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["columns"] == ["user", "n"]
    assert data["rows"] == [{"user": "alice", "n": 1}]
    assert data["truncated"] is False


def test_write_rejected(client):
    resp = client.post("/api/query", headers=AUTH, json={"sql": "DELETE FROM audit_logs"})
    assert resp.status_code == 400
    assert "Only SELECT" in resp.json()["detail"]


def test_bad_schema_rejected(client):
    resp = client.post("/api/query", headers=AUTH, json={"sql": "SELECT * FROM sqlite_master"})
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]


def test_missing_sql_field(client):
    resp = client.post("/api/query", headers=AUTH, json={})
    assert resp.status_code == 422
