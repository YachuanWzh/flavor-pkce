"""Test PKCE /authorize endpoint."""
import os
import json
import tempfile
import hashlib
import base64
import secrets

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db, get_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    """Use a temporary database for each test."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="auth_test_")
    os.close(fd)
    config.DB_PATH = tmp_path
    config.SEED_TEST_USER = True
    init_db()
    yield
    config.DB_PATH = _original_db_path
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


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


# ---------------------------------------------------------------------------
# redirect_uri exact matching (P0-5)
# ---------------------------------------------------------------------------

def test_redirect_uri_rejects_host_suffix_attack(client):
    """A redirect_uri that only *prefix-matches* the registered value must be
    rejected.

    The old prefix-match (`startswith("http://127.0.0.1:")`) accepted
    'http://127.0.0.1:9999.evil.com/callback' because it starts with the
    registered string — a parser-differential open-redirect vector.
    """
    params, _, _ = make_pkce_params()
    params["redirect_uri"] = "http://127.0.0.1:9999.evil.com/callback"
    session = login_and_get_session(client)
    resp = client.get("/authorize", params=params, cookies={"session_token": session})
    assert resp.status_code == 400


def test_redirect_uri_rejects_wrong_scheme(client):
    """https variant of the registered host must be rejected (scheme must match)."""
    params, _, _ = make_pkce_params()
    params["redirect_uri"] = "https://127.0.0.1:9999/callback"
    session = login_and_get_session(client)
    resp = client.get("/authorize", params=params, cookies={"session_token": session})
    assert resp.status_code == 400


def test_redirect_uri_accepts_registered_host_any_port(client):
    """The seeded CLI client registers 'http://127.0.0.1:*' — any port on the
    loopback host must be accepted (native-app callback with random port)."""
    params, _, _ = make_pkce_params()
    params["redirect_uri"] = "http://127.0.0.1:45678/callback"
    session = login_and_get_session(client)
    resp = client.get("/authorize", params=params, cookies={"session_token": session})
    assert resp.status_code == 200


def test_validate_redirect_uri_exact_match():
    """Exact registered URIs match only themselves — no prefix leakage."""
    from auth_server.main import validate_redirect_uri

    registered = json.dumps(["http://app.example.com/cb"])
    assert validate_redirect_uri(registered, "http://app.example.com/cb") is True
    assert validate_redirect_uri(registered, "http://app.example.com/cb/extra") is False
    assert validate_redirect_uri(registered, "http://app.example.com/cbx") is False
    assert validate_redirect_uri(registered, "http://evil.example.com/cb") is False
