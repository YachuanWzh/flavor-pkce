"""Test that sessions and pending-authorization state are persisted in SQLite
(instead of in-process dicts), so they survive restarts and scale horizontally.
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db, get_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    """Use a temporary database for each test."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="sess_test_")
    os.close(fd)
    config.DB_PATH = tmp_path
    init_db()
    yield
    config.DB_PATH = _original_db_path
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except PermissionError:
            pass


@pytest.fixture
def client():
    from auth_server.main import app
    return TestClient(app)


def _login(client, username="alice", password="Secret123"):
    client.post("/register", json={"username": username, "password": password})
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 200
    return resp.json()["session_token"]


def test_init_db_creates_session_tables():
    db = get_db()
    tables = {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    db.close()
    assert "sessions" in tables
    assert "pending_auths" in tables


def test_login_persists_session_row(client):
    """A successful login must write the session to the DB, readable by a
    brand-new connection (simulating another process / restart)."""
    token = _login(client)

    db = get_db()
    row = db.execute(
        "SELECT * FROM sessions WHERE session_token = ?", (token,)
    ).fetchone()
    db.close()
    assert row is not None
    assert row["user_id"]
    assert row["username"] == "alice"


def test_register_persists_session_row(client):
    """Auto-login after registration must also persist the session."""
    resp = client.post("/register", json={"username": "bob", "password": "Secret123"})
    assert resp.status_code == 201
    cookie = resp.headers["Set-Cookie"]
    assert "session_token=" in cookie
    token = cookie.split("session_token=")[1].split(";")[0]

    db = get_db()
    row = db.execute(
        "SELECT * FROM sessions WHERE session_token = ?", (token,)
    ).fetchone()
    db.close()
    assert row is not None
    assert row["username"] == "bob"


def test_get_session_returns_none_for_unknown_token():
    from auth_server.database import get_session
    assert get_session("does-not-exist") is None


def test_expired_session_rejected():
    """An expired session must be rejected and deleted."""
    from datetime import datetime, timezone, timedelta
    from auth_server.database import create_session, get_session

    db = get_db()
    db.execute(
        "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
        ("u1", "alice", "x"),
    )
    db.commit()
    db.close()

    create_session(
        session_token="expired-token",
        user_id="u1",
        username="alice",
        expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    assert get_session("expired-token") is None
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_token = ?", ("expired-token",)
    ).fetchone()
    db.close()
    assert row[0] == 0


def test_authorize_persists_pending_auth(client):
    """GET /authorize while logged in must persist pending_auth for the session."""
    token = _login(client)

    params = {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "code_challenge": "test-challenge",
        "code_challenge_method": "S256",
        "state": "test-state",
    }
    resp = client.get("/authorize", params=params, cookies={"session_token": token})
    assert resp.status_code == 200

    db = get_db()
    row = db.execute(
        "SELECT * FROM pending_auths WHERE session_token = ?", (token,)
    ).fetchone()
    db.close()
    assert row is not None
    assert row["client_id"] == "flavor-code-cli"
    assert row["state"] == "test-state"


def test_consent_reads_pending_auth_from_db(client):
    """Consent must consume the persisted pending_auth, not in-memory state."""
    token = _login(client)

    params = {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "code_challenge": "test-challenge",
        "code_challenge_method": "S256",
        "state": "test-state",
    }
    client.get("/authorize", params=params, cookies={"session_token": token})
    resp = client.post(
        "/consent", follow_redirects=False, cookies={"session_token": token}
    )
    assert resp.status_code == 302
    assert "code=" in resp.headers["location"]
    assert "state=test-state" in resp.headers["location"]
