"""Refresh token lifecycle, RFC 7009 revocation, and revocation checks (P0-3)."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db, get_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="refresh_test_")
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


def _do_pkce_login(client):
    """Run the full PKCE flow and return (access, refresh, jti)."""
    import hashlib
    import base64
    import secrets
    import urllib.parse

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
    assert session
    auth_resp = client.get("/authorize", params=params, cookies={"session_token": session})
    assert auth_resp.status_code == 200
    consent = client.post(
        "/consent", cookies={"session_token": session}, follow_redirects=False
    )
    assert consent.status_code == 302
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(consent.headers["location"]).query)
    code = qs["code"][0]

    tok = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })
    assert tok.status_code == 200, tok.text
    body = tok.json()
    return body["access_token"], body.get("refresh_token"), body.get("config_version")


def _jti_of(token: str) -> str:
    import jwt as pyjwt
    return pyjwt.decode(token, options={"verify_signature": False})["jti"]


def test_token_response_includes_refresh_token(client):
    _, refresh, _ = _do_pkce_login(client)
    assert refresh and len(refresh) >= 32


def test_refresh_rotates_tokens(client):
    _, refresh, _ = _do_pkce_login(client)
    resp = client.post("/refresh", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "flavor-code-cli",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["refresh_token"] != refresh
    assert body["token_type"] == "Bearer"


def test_reusing_old_refresh_rejected(client):
    _, refresh, _ = _do_pkce_login(client)
    # First rotation consumes the old refresh
    resp = client.post("/refresh", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "flavor-code-cli",
    })
    assert resp.status_code == 200
    # Reusing the same (now consumed) refresh must fail
    resp = client.post("/refresh", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "flavor-code-cli",
    })
    assert resp.status_code == 400
    assert "invalid_grant" in resp.text


def test_revoked_refresh_token_rejected(client):
    _, refresh, _ = _do_pkce_login(client)
    resp = client.post("/revoke", data={
        "token": refresh,
        "token_type_hint": "refresh_token",
        "client_id": "flavor-code-cli",
    })
    assert resp.status_code == 200
    resp = client.post("/refresh", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": "flavor-code-cli",
    })
    assert resp.status_code == 400
    assert "invalid_grant" in resp.text


def test_revoke_requires_valid_token(client):
    resp = client.post("/revoke", data={"token": "bogus", "client_id": "flavor-code-cli"})
    # RFC 7009: invalid token still returns 200 (to avoid token scanning)
    assert resp.status_code == 200


def test_internal_revoked_check_for_access_token(client):
    access, _, _ = _do_pkce_login(client)
    jti = _jti_of(access)
    headers = {"X-Internal-Service-Token": config.INTERNAL_SERVICE_TOKEN}

    # Not revoked yet
    resp = client.get("/internal/tokens/revoked", params={"jti": jti}, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"revoked": False}

    # Revoke the access token by jti
    resp = client.post("/revoke", data={"token": access, "client_id": "flavor-code-cli"})
    assert resp.status_code == 200

    resp = client.get("/internal/tokens/revoked", params={"jti": jti}, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"revoked": True}


def test_internal_revoked_requires_service_token(client):
    resp = client.get("/internal/tokens/revoked", params={"jti": "whatever"})
    assert resp.status_code == 401


def test_access_token_revocation_recorded(client):
    """Revoking an access token marks its jti revoked in the tokens table."""
    access, _, _ = _do_pkce_login(client)
    jti = _jti_of(access)
    client.post("/revoke", data={"token": access, "client_id": "flavor-code-cli"})
    db = get_db()
    row = db.execute("SELECT revoked FROM tokens WHERE jti = ?", (jti,)).fetchone()
    db.close()
    assert row is not None
    assert row["revoked"] == 1
