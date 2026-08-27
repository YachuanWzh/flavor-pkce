"""API tests for the data-agent knowledge endpoints (QA / glossary / presets)."""

import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="knowledge_api_test_")
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


# ---- QA pairs -------------------------------------------------------------


def test_qa_requires_auth(client):
    resp = client.get("/api/agent/qa")
    assert resp.status_code == 401


def test_qa_crud_cycle(client):
    resp = client.post("/api/agent/qa", headers=AUTH, json={
        "question": "统计上个月的销售额",
        "sql_template": "SELECT COUNT(*) AS n FROM audit_logs",
        "tags": ["sales"],
    })
    assert resp.status_code == 200
    pair = resp.json()
    assert pair["enabled"] is True

    resp = client.get("/api/agent/qa", headers=AUTH)
    assert resp.status_code == 200
    assert [p["question"] for p in resp.json()["items"]] == ["统计上个月的销售额"]

    resp = client.delete(f"/api/agent/qa/{pair['id']}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    resp = client.delete(f"/api/agent/qa/{pair['id']}", headers=AUTH)
    assert resp.status_code == 404


def test_qa_upsert_validation(client):
    resp = client.post("/api/agent/qa", headers=AUTH, json={
        "question": "  ", "sql_template": "SELECT 1",
    })
    assert resp.status_code == 400


# ---- Column glossary -------------------------------------------------------


def test_glossary_requires_auth(client):
    resp = client.get("/api/agent/glossary")
    assert resp.status_code == 401


def test_glossary_crud_cycle(client):
    resp = client.post("/api/agent/glossary", headers=AUTH, json={
        "table_name": "audit_logs",
        "column_name": "status",
        "business_name": "HTTP 状态码",
        "synonyms": ["状态"],
        "description": "200=成功, 4xx=客户端错误",
    })
    assert resp.status_code == 200
    entry = resp.json()
    assert entry["table_name"] == "audit_logs"

    resp = client.get("/api/agent/glossary", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1

    resp = client.delete(f"/api/agent/glossary/{entry['id']}", headers=AUTH)
    assert resp.status_code == 200
    resp = client.delete(f"/api/agent/glossary/{entry['id']}", headers=AUTH)
    assert resp.status_code == 404


# ---- Preset questions ------------------------------------------------------


def test_presets_requires_auth(client):
    resp = client.get("/api/agent/presets")
    assert resp.status_code == 401


def test_presets_crud_and_enabled_filter(client):
    resp = client.post("/api/agent/presets", headers=AUTH, json={
        "question": "How many requests today?",
        "sort_order": 1,
    })
    assert resp.status_code == 200
    resp = client.post("/api/agent/presets", headers=AUTH, json={
        "question": "Top models this week",
        "sort_order": 0,
        "enabled": False,
    })
    assert resp.status_code == 200

    resp = client.get("/api/agent/presets", headers=AUTH)
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 2

    resp = client.get("/api/agent/presets?enabled_only=true", headers=AUTH)
    assert resp.status_code == 200
    assert [p["question"] for p in resp.json()["items"]] == ["How many requests today?"]

    preset = resp.json()["items"][0]
    resp = client.delete(f"/api/agent/presets/{preset['id']}", headers=AUTH)
    assert resp.status_code == 200
    resp = client.delete(f"/api/agent/presets/{preset['id']}", headers=AUTH)
    assert resp.status_code == 404


def test_presets_upsert_validation(client):
    resp = client.post("/api/agent/presets", headers=AUTH, json={"question": "   "})
    assert resp.status_code == 400
