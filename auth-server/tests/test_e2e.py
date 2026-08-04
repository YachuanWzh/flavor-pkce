"""End-to-end integration test for the full PKCE flow."""
import os
import hashlib
import base64
import secrets
import tempfile
import urllib.parse

import pytest
from fastapi.testclient import TestClient

import auth_server.config as auth_config


@pytest.fixture(autouse=True)
def setup():
    """Use a temp DB."""
    fd, tmp_db = tempfile.mkstemp(suffix=".db", prefix="e2e_test_")
    os.close(fd)
    old_db = auth_config.DB_PATH
    auth_config.DB_PATH = tmp_db

    from auth_server.database import init_db
    init_db()

    yield

    auth_config.DB_PATH = old_db
    try:
        os.remove(tmp_db)
    except PermissionError:
        pass


def make_pkce_params():
    """Generate valid PKCE parameters."""
    code_verifier = secrets.token_urlsafe(32)[:43]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)[:43]
    redirect_uri = "http://127.0.0.1:9999/callback"
    return code_verifier, code_challenge, state, redirect_uri


def test_full_pkce_e2e():
    """
    Full end-to-end PKCE flow:
    1. Login
    2. GET /authorize → consent page
    3. POST /consent → redirect with code
    4. POST /token → JWT access_token
    5. Verify JWT with gateway's verify function
    """
    # Ensure keys exist before importing gateway
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()

    # Point gateway to the same public key path used by auth-server
    os.environ["JWT_PUBLIC_KEY_PATH"] = auth_config.JWT_PUBLIC_KEY_PATH

    from auth_server.main import app as auth_app
    from gateway.main import verify_jwt

    auth_client = TestClient(auth_app)

    code_verifier, code_challenge, state, redirect_uri = make_pkce_params()

    # Step 1: Login
    resp = auth_client.post("/login", data={
        "username": "testuser",
        "password": "testpass",
    })
    assert resp.status_code == 200

    # Step 2: GET /authorize
    params = {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "models:read models:use",
    }
    resp = auth_client.get("/authorize", params=params)
    assert resp.status_code == 200
    assert "Authorization Request" in resp.text

    # Step 3: POST /consent
    resp = auth_client.post("/consent", follow_redirects=False)
    assert resp.status_code == 302

    location = resp.headers["location"]
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    code = qs["code"][0]
    returned_state = qs["state"][0]
    assert returned_state == state

    # Step 4: POST /token
    resp = auth_client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == 259200

    access_token = data["access_token"]

    # Step 5: Verify JWT with gateway's verify function
    payload = verify_jwt(access_token)
    assert payload is not None
    assert payload["sub"] is not None
    assert payload["client_id"] == "flavor-code-cli"
    assert "exp" in payload
    assert "iat" in payload
    assert "jti" in payload


def test_full_flow_wrong_code_verifier_rejected():
    """E2E flow with wrong code_verifier must be rejected at /token."""
    from auth_server.main import app as auth_app

    auth_client = TestClient(auth_app)

    code_verifier, code_challenge, state, redirect_uri = make_pkce_params()

    # Login
    auth_client.post("/login", data={"username": "testuser", "password": "testpass"})

    # Authorize
    params = {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    auth_client.get("/authorize", params=params)

    # Consent
    resp = auth_client.post("/consent", follow_redirects=False)
    location = resp.headers["location"]
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    code = qs["code"][0]

    # Token with wrong verifier
    resp = auth_client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": "flavor-code-cli",
        "code_verifier": "wrong-verifier",
    })
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"
