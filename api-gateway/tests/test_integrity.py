"""Audit-log API auth + hash-chain tamper detection (P0-1)."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_log, verify_integrity


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="integrity_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    old_token = config.AUDIT_API_TOKEN
    config.AUDIT_API_TOKEN = "test-audit-token"
    init_audit_db()
    yield
    config.AUDIT_DB_PATH = old
    config.AUDIT_API_TOKEN = old_token
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except PermissionError:
            pass


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


def _sample_log(user="alice"):
    insert_log(
        timestamp="2026-07-20T10:00:00+00:00",
        user=user, method="POST", path="/v1/chat/completions",
        status=200, duration_ms=100.0, upstream_ms=90.0, level="INFO",
    )


# ---------------------------------------------------------------------------
# Hash-chain integrity
# ---------------------------------------------------------------------------

def test_verify_integrity_passes_for_untampered_chain():
    _sample_log("alice")
    _sample_log("bob")
    assert verify_integrity() is True


def test_verify_integrity_detects_tampering():
    _sample_log("alice")
    _sample_log("bob")
    import sqlite3
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    conn.execute(
        "UPDATE audit_logs SET request_body = 'tampered' WHERE id = 1"
    )
    conn.commit()
    conn.close()
    assert verify_integrity() is False


def test_verify_integrity_empty_chain_is_valid():
    assert verify_integrity() is True


def test_insert_log_hashes_are_chained():
    _sample_log("alice")
    _sample_log("bob")
    import sqlite3
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    rows = conn.execute(
        "SELECT id, prev_hash, hash FROM audit_logs ORDER BY id"
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    assert rows[0][1] is None or rows[0][1] == ""  # genesis row
    assert rows[1][1] == rows[0][2]  # second row chains to first


# ---------------------------------------------------------------------------
# API auth
# ---------------------------------------------------------------------------

def test_api_logs_requires_token(client):
    resp = client.get("/api/logs")
    assert resp.status_code == 401


def test_api_logs_with_token(client):
    resp = client.get("/api/logs", headers={"X-Audit-Token": "test-audit-token"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_api_log_detail_requires_token(client):
    resp = client.get("/api/logs/1")
    assert resp.status_code == 401


def test_delete_logs_requires_token(client):
    resp = client.delete("/api/logs")
    assert resp.status_code == 401


def test_delete_logs_with_token(client):
    _sample_log()
    resp = client.delete("/api/logs", headers={"X-Audit-Token": "test-audit-token"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1


def test_integrity_endpoint_reports_valid(client):
    _sample_log()
    resp = client.get("/api/logs/integrity", headers={"X-Audit-Token": "test-audit-token"})
    assert resp.status_code == 200
    assert resp.json() == {"valid": True}


def test_integrity_endpoint_detects_tampering(client):
    _sample_log()
    import sqlite3
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    conn.execute("UPDATE audit_logs SET status = 500 WHERE id = 1")
    conn.commit()
    conn.close()
    resp = client.get("/api/logs/integrity", headers={"X-Audit-Token": "test-audit-token"})
    assert resp.status_code == 200
    assert resp.json() == {"valid": False}


def test_audit_page_still_public(client):
    """The HTML viewer stays readable (the data API is what requires auth)."""
    resp = client.get("/audit")
    assert resp.status_code == 200
