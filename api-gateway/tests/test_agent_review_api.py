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
