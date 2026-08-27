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


@pytest.fixture(autouse=True)
def setup_keys():
    """Point the gateway at the auth-server public key so JWT verification works."""
    keys_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-server", "keys"
    )
    if not os.path.exists(keys_dir):
        os.makedirs(keys_dir, exist_ok=True)
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auth-server"))
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()
    old_path = config.JWT_PUBLIC_KEY_PATH
    config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
    import gateway.main as gm
    gm._public_key = None
    yield
    config.JWT_PUBLIC_KEY_PATH = old_path
    gm._public_key = None


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


def _make_jwt(role: str, username: str = "admin") -> str:
    """Sign a JWT with the auth-server private key for the given role."""
    import time
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    private_key_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-server", "keys", "private.pem"
    )
    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())

    now = int(time.time())
    payload = {
        "sub": "u-admin", "username": username, "client_id": "flavor-code-cli",
        "scope": "models:use", "role": role,
        "iat": now, "exp": now + 3600, "jti": "admin-jti-1",
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


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


def test_verify_integrity_tolerates_purged_chain_head():
    """Retention deletes the oldest rows; the surviving head's prev_hash then
    points at a purged predecessor. The chain must still verify from the
    first surviving row forward (head-truncation is not distinguishable
    from a purge — documented trade-off of retention)."""
    _sample_log("alice")
    _sample_log("bob")
    _sample_log("carol")
    import sqlite3
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    conn.execute("DELETE FROM audit_logs WHERE id = 1")  # simulate purge
    conn.commit()
    conn.close()
    assert verify_integrity() is True


def test_verify_integrity_detects_tampering_after_head_purge():
    _sample_log("alice")
    _sample_log("bob")
    _sample_log("carol")
    import sqlite3
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    conn.execute("DELETE FROM audit_logs WHERE id = 1")
    conn.execute("UPDATE audit_logs SET status = 599 WHERE id = 2")
    conn.commit()
    conn.close()
    assert verify_integrity() is False


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


# ---------------------------------------------------------------------------
# Admin-JWT auth (SSO-style)
# ---------------------------------------------------------------------------

def test_api_logs_with_admin_jwt(client):
    """A signed JWT with role=admin should be able to read audit logs."""
    token = _make_jwt("admin")
    resp = client.get("/api/logs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_api_logs_rejects_non_admin_jwt(client):
    """A signed JWT with role=user should be forbidden (403)."""
    token = _make_jwt("user", username="alice")
    resp = client.get("/api/logs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_api_logs_rejects_invalid_jwt(client):
    """A tampered/unsigned JWT should be rejected (401)."""
    resp = client.get("/api/logs", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_delete_logs_with_admin_jwt(client):
    """DELETE /api/logs should work with an admin JWT."""
    _sample_log()
    token = _make_jwt("admin")
    resp = client.delete("/api/logs", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1


def test_api_logs_with_admin_cookie(client):
    """SSO: an admin JWT in the access_token HttpOnly cookie should work."""
    token = _make_jwt("admin")
    client.cookies.set("access_token", token)
    resp = client.get("/api/logs")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_api_logs_rejects_non_admin_cookie(client):
    """SSO: a user-role JWT in the cookie should be forbidden (403)."""
    token = _make_jwt("user", username="alice")
    client.cookies.set("access_token", token)
    resp = client.get("/api/logs")
    assert resp.status_code == 403


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
