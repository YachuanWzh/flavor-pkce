"""Auth-server audit events and retention policy (P0-10)."""
import os
import tempfile
from datetime import datetime, timezone, timedelta

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db, get_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="audit_test_")
    os.close(fd)
    config.DB_PATH = tmp_path
    config.SEED_TEST_USER = True
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


def _events(client, event: str | None = None) -> list[dict]:
    db = get_db()
    if event:
        rows = db.execute(
            "SELECT * FROM audit_logs WHERE event = ? ORDER BY id", (event,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM audit_logs ORDER BY id").fetchall()
    db.close()
    return [dict(r) for r in rows]


def test_login_success_is_audited(client):
    resp = client.post("/login", data={"username": "testuser", "password": "testpass"})
    assert resp.status_code == 200
    events = _events(client, "login.success")
    assert len(events) == 1
    assert events[0]["actor_username"] == "testuser"
    assert events[0]["ip"]


def test_login_failure_is_audited(client):
    resp = client.post("/login", data={"username": "testuser", "password": "Wrong1"})
    assert resp.status_code == 401
    events = _events(client, "login.failed")
    assert len(events) == 1
    assert events[0]["actor_username"] == "testuser"


def test_register_is_audited(client):
    resp = client.post("/register", json={"username": "newbie", "password": "Secret123"})
    assert resp.status_code == 201
    events = _events(client, "register")
    assert len(events) == 1
    assert events[0]["actor_username"] == "newbie"


def test_token_exchange_is_audited(client):
    import hashlib, base64, secrets, urllib.parse
    code_verifier = secrets.token_urlsafe(32)[:43]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)[:43]
    params = {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "models:read models:use",
    }
    login = client.post("/login", data={"username": "testuser", "password": "testpass"})
    session = login.cookies.get("session_token")
    client.get("/authorize", params=params, cookies={"session_token": session})
    consent = client.post("/consent", cookies={"session_token": session}, follow_redirects=False)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(consent.headers["location"]).query)
    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": qs["code"][0],
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })
    assert resp.status_code == 200

    events = _events(client, "token.exchange")
    assert len(events) == 1
    assert events[0]["detail"] and "flavor-code-cli" in events[0]["detail"]


def test_refresh_and_revoke_are_audited(client):
    import hashlib, base64, secrets, urllib.parse
    code_verifier = secrets.token_urlsafe(32)[:43]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)[:43]
    params = {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "models:use",
    }
    login = client.post("/login", data={"username": "testuser", "password": "testpass"})
    session = login.cookies.get("session_token")
    client.get("/authorize", params=params, cookies={"session_token": session})
    consent = client.post("/consent", cookies={"session_token": session}, follow_redirects=False)
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(consent.headers["location"]).query)
    tok = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": qs["code"][0],
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })
    refresh = tok.json()["refresh_token"]

    client.post("/refresh", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "flavor-code-cli",
    })
    assert len(_events(client, "refresh")) == 1

    client.post("/revoke", data={"token": refresh, "client_id": "flavor-code-cli"})
    assert len(_events(client, "revoke")) == 1


def test_purge_removes_expired_logs(client):
    db = get_db()
    old = (datetime.now(timezone.utc) - timedelta(days=config.AUDIT_RETENTION_DAYS + 10)).isoformat()
    db.execute(
        """INSERT INTO audit_logs (event, actor_username, ip, detail, created_at)
           VALUES ('login.success', 'olduser', '1.1.1.1', '{}', ?)""", (old,),
    )
    fresh = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO audit_logs (event, actor_username, ip, detail, created_at)
           VALUES ('login.success', 'newuser', '2.2.2.2', '{}', ?)""", (fresh,),
    )
    db.commit()
    db.close()

    from auth_server.audit import purge_old_audit_logs
    deleted = purge_old_audit_logs()
    assert deleted >= 1
    events = _events(client)
    assert all(e["actor_username"] != "olduser" for e in events)


def test_audit_logs_table_created_by_init_db():
    db = get_db()
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    db.close()
    assert "audit_logs" in tables
