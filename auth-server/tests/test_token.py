"""Test PKCE /token endpoint and JWT signing."""
import os
import tempfile
import hashlib
import base64
import secrets

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db, get_db
import auth_server.config as config


@pytest.fixture(autouse=True)
def clean_db_and_keys():
    """Use temp DB and clean keys before each test."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="auth_test_")
    os.close(fd)
    config.DB_PATH = tmp_path
    config.SEED_TEST_USER = True

    # Clean up test keys
    keys_dir = os.path.join(os.path.dirname(__file__), "..", "auth_server", "keys")
    if os.path.exists(keys_dir):
        for f in os.listdir(keys_dir):
            if f.endswith(".pem"):
                os.remove(os.path.join(keys_dir, f))

    init_db()
    yield
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except PermissionError:
        pass  # Windows may still hold the file handle briefly


@pytest.fixture
def client():
    from auth_server.main import app
    return TestClient(app)


def make_pkce_params():
    """Generate valid PKCE parameters with a matching code_verifier."""
    code_verifier = secrets.token_urlsafe(32)[:43]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)[:43]
    return code_verifier, code_challenge, state


def complete_authorization_flow(client):
    """Helper: full authorize + consent flow, returns code and code_verifier."""
    code_verifier, code_challenge, state = make_pkce_params()

    # Login
    client.post("/login", data={"username": "testuser", "password": "testpass"})

    # /authorize
    params = {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "models:read models:use",
    }
    auth_resp = client.get("/authorize", params=params)
    assert auth_resp.status_code == 200

    # /consent
    consent_resp = client.post("/consent", follow_redirects=False)
    assert consent_resp.status_code == 302

    # Extract code from redirect URL
    import urllib.parse
    location = consent_resp.headers["location"]
    parsed = urllib.parse.urlparse(location)
    qs = urllib.parse.parse_qs(parsed.query)
    return qs["code"][0], code_verifier, state


def test_token_exchange_success(client):
    """POST /token with valid code should return JWT access_token."""
    code, code_verifier, _ = complete_authorization_flow(client)

    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })

    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "Bearer"
    assert "expires_in" in data
    assert data["expires_in"] == 259200
    assert data["config_version"] >= 1
    assert data["llm_config"]["base_url"].startswith("http")
    assert data["llm_config"]["default_model"]
    assert "upstream_url" not in data["llm_config"]
    assert "upstream_api_key" not in data["llm_config"]

    # Verify it's a JWT (3 parts separated by dots)
    access_token = data["access_token"]
    parts = access_token.split(".")
    assert len(parts) == 3
    import jwt
    decoded = jwt.decode(access_token, options={"verify_signature": False})
    assert decoded["config_version"] == data["config_version"]


def test_token_exchange_wrong_code_verifier(client):
    """POST /token with wrong code_verifier should return 400."""
    code, _, _ = complete_authorization_flow(client)

    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": "wrong-verifier-value-1234567890123456789012345678901234567890",
    })

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_exchange_expired_code(client):
    """POST /token with expired code should return 400."""
    code, code_verifier, _ = complete_authorization_flow(client)

    # Manually expire the code
    db = get_db()
    db.execute(
        "UPDATE authorization_codes SET expires_at = ? WHERE code = ?",
        ("2000-01-01T00:00:00", code)
    )
    db.commit()
    db.close()

    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_exchange_reused_code(client):
    """POST /token with already-used code should return 400."""
    code, code_verifier, _ = complete_authorization_flow(client)

    # First use
    resp1 = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })
    assert resp1.status_code == 200

    # Second use (same code)
    resp2 = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })

    assert resp2.status_code == 400
    assert resp2.json()["error"] == "invalid_grant"


def test_token_exchange_wrong_redirect_uri(client):
    """POST /token with different redirect_uri should return 400."""
    code, code_verifier, _ = complete_authorization_flow(client)

    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:8888/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_exchange_wrong_client_id(client):
    """POST /token with different client_id should return 400."""
    code, code_verifier, _ = complete_authorization_flow(client)

    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "wrong-client",
        "code_verifier": code_verifier,
    })

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_exchange_invalid_grant_type(client):
    """POST /token with invalid grant_type should return 400."""
    resp = client.post("/token", data={
        "grant_type": "password",
        "code": "some-code",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": "verifier",
    })

    assert resp.status_code == 400


def test_token_stored_with_jti(client):
    """Successful token exchange should store token with jti in DB."""
    code, code_verifier, _ = complete_authorization_flow(client)

    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })
    assert resp.status_code == 200

    # Verify token is stored in DB
    import jwt
    access_token = resp.json()["access_token"]

    # Decode without verification to get jti
    decoded = jwt.decode(access_token, options={"verify_signature": False})
    jti = decoded["jti"]

    db = get_db()
    row = db.execute("SELECT * FROM tokens WHERE jti = ?", (jti,)).fetchone()
    db.close()
    assert row is not None
    assert row["client_id"] == "flavor-code-cli"
    assert row["revoked"] == 0


def test_jwt_contains_username(client):
    """The JWT payload should include a 'username' claim."""
    code, code_verifier, _state = complete_authorization_flow(client)

    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "client_id": "flavor-code-cli",
        "code_verifier": code_verifier,
    })
    assert resp.status_code == 200

    import jwt
    access_token = resp.json()["access_token"]
    decoded = jwt.decode(access_token, options={"verify_signature": False})
    assert decoded.get("username") == "testuser"
