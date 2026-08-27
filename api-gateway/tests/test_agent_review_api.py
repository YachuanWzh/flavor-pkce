"""API tests for /api/agent/metrics and /api/agent/stats."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_agent_query


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="agent_review_test_")
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
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auth-server"))
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()
    config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
    yield


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


AUTH = {"X-Audit-Token": "test-audit-token"}


def test_metrics_requires_auth(client):
    resp = client.get("/api/agent/metrics")
    assert resp.status_code == 401


def test_metrics_upsert_and_list(client):
    resp = client.post(
        "/api/agent/metrics",
        headers=AUTH,
        json={"term": "gmv", "definition": "gross merchandise value", "synonyms": ["流水", "交易额"]},
    )
    assert resp.status_code == 200
    assert resp.json()["term"] == "gmv"

    resp = client.get("/api/agent/metrics", headers=AUTH)
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["term"] == "gmv"


def test_metrics_delete(client):
    resp = client.post(
        "/api/agent/metrics", headers=AUTH,
        json={"term": "gmv", "definition": "d", "synonyms": []},
    )
    term_id = resp.json()["id"]
    resp = client.delete(f"/api/agent/metrics/{term_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert client.get("/api/agent/metrics", headers=AUTH).json()["items"] == []


def test_agent_stats_aggregates(client):
    insert_agent_query(
        timestamp="2026-08-01T10:00:00+00:00", user="alice",
        question="q1", sql="SELECT 1", status="success", duration_ms=100.0,
        rows_returned=5, prompt_tokens=10, completion_tokens=20, model="m1",
    )
    insert_agent_query(
        timestamp="2026-08-01T11:00:00+00:00", user="alice",
        question="q2", sql="SELECT bad", status="error", duration_ms=50.0,
        error="syntax error",
    )
    insert_agent_query(
        timestamp="2026-08-02T10:00:00+00:00", user="bob",
        question="q3", sql="DELETE x", status="blocked", duration_ms=0.0,
        error="dangerous sql",
    )

    resp = client.get("/api/agent/stats", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["success"] == 1
    assert data["error"] == 1
    assert data["blocked"] == 1
    assert data["daily"][0]["date"] == "2026-08-01"
    assert data["daily"][0]["total"] == 2
    assert data["error_top"][0]["count"] >= 1
