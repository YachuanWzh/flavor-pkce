"""Test PKCE /authorize endpoint."""
import os
import hashlib
import base64
import secrets

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db, DB_PATH, get_db
import auth_server.config as config


@pytest.fixture(autouse=True)
def clean_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    config.DB_PATH = DB_PATH
    init_db()
    yield
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


@pytest.fixture
def client():
    from auth_server.main import app
    return TestClient(app)


def make_pkce_params():
    """Generate valid PKCE parameters."""
    code_verifier = secrets.token_urlsafe(32)[:43]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)[:43]
    return {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "models:read models:use",
    }, code_verifier, state


def login_and_get_session(client):
    """Helper: login as testuser and return session cookie."""
    resp = client.post("/login", data={
        "username": "testuser",
        "password": "testpass"
    })
    return resp.cookies.get("session_token")


def test_authorize_redirects_to_login_when_not_authenticated(client):
    """GET /authorize without session should redirect to /login."""
    params, _, _ = make_pkce_params()
    resp = client.get("/authorize", params=params, follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]


def test_authorize_shows_consent_when_authenticated(client):
    """GET /authorize with valid session should show consent page."""
    params, _, _ = make_pkce_params()
    session = login_and_get_session(client)
    resp = client.get("/authorize", params=params, cookies={"session_token": session})
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "").lower()


def test_authorize_rejects_missing_params(client):
    """GET /authorize without required params should return 400."""
    session = login_and_get_session(client)
    resp = client.get("/authorize", params={"response_type": "code"},
                      cookies={"session_token": session})
    assert resp.status_code == 400


def test_authorize_rejects_invalid_client(client):
    """GET /authorize with unknown client_id should return 400."""
    params, _, _ = make_pkce_params()
    params["client_id"] = "unknown-client"
    session = login_and_get_session(client)
    resp = client.get("/authorize", params=params, cookies={"session_token": session})
    assert resp.status_code == 400


def test_consent_redirects_with_code_and_state(client):
    """POST /consent should redirect with code and state."""
    params, code_verifier, state = make_pkce_params()
    session = login_and_get_session(client)

    # First call /authorize to store params in session
    auth_resp = client.get("/authorize", params=params,
                           cookies={"session_token": session})
    assert auth_resp.status_code == 200

    # Now POST /consent
    consent_resp = client.post("/consent", cookies={"session_token": session},
                               follow_redirects=False)
    assert consent_resp.status_code == 302

    location = consent_resp.headers["location"]
    assert f"state={state}" in location
    assert "code=" in location

    # Verify code is stored in DB
    import urllib.parse
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    code = qs["code"][0]

    db = get_db()
    row = db.execute(
        "SELECT * FROM authorization_codes WHERE code = ?", (code,)
    ).fetchone()
    db.close()
    assert row is not None
    assert row["client_id"] == "flavor-code-cli"
    assert row["code_challenge"] == params["code_challenge"]
    assert row["used"] == 0


def test_authorize_rejects_invalid_redirect_uri(client):
    """GET /authorize with non-matching redirect_uri should fail."""
    params, _, _ = make_pkce_params()
    params["redirect_uri"] = "http://evil.com/callback"
    session = login_and_get_session(client)
    resp = client.get("/authorize", params=params, cookies={"session_token": session})
    assert resp.status_code == 400


def test_consent_without_authorize_fails(client):
    """POST /consent without prior /authorize call should fail."""
    session = login_and_get_session(client)
    resp = client.post("/consent", cookies={"session_token": session})
    assert resp.status_code == 400
