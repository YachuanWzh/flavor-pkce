"""Test user registration and login."""
import os
import tempfile
import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    """Use a temporary database for each test."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="auth_test_")
    os.close(fd)
    config.DB_PATH = tmp_path
    init_db()
    yield
    config.DB_PATH = _original_db_path
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


@pytest.fixture
def client():
    """Create test client."""
    from auth_server.main import app
    return TestClient(app)


def test_register_new_user(client):
    """POST /register should create a new user."""
    resp = client.post("/register", json={
        "username": "alice",
        "password": "Secret123"
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["username"] == "alice"
    assert "id" in data
    assert "password" not in data
    assert "password_hash" not in data


def test_register_duplicate_username(client):
    """POST /register should reject duplicate usernames."""
    client.post("/register", json={
        "username": "alice",
        "password": "Secret123"
    })
    resp = client.post("/register", json={
        "username": "alice",
        "password": "Another456"
    })
    assert resp.status_code == 409


def test_register_missing_fields(client):
    """POST /register should reject missing fields."""
    resp = client.post("/register", json={"username": "bob"})
    assert resp.status_code == 400  # Converted from 422 by global handler


def test_login_success(client):
    """POST /login (API) should set session cookie on success."""
    client.post("/register", json={
        "username": "charlie",
        "password": "MyPass123"
    })
    resp = client.post("/login", data={
        "username": "charlie",
        "password": "MyPass123"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" not in data  # login returns session, not JWT
    assert "session_token" in data
    assert "Set-Cookie" in resp.headers


def test_login_wrong_password(client):
    """POST /login should reject wrong password."""
    client.post("/register", json={
        "username": "dave",
        "password": "Correct1"
    })
    resp = client.post("/login", data={
        "username": "dave",
        "password": "wrong"
    })
    assert resp.status_code == 401


def test_login_nonexistent_user(client):
    """POST /login should reject non-existent users."""
    resp = client.post("/login", data={
        "username": "nobody",
        "password": "whatever"
    })
    assert resp.status_code == 401


def test_login_page_returns_html(client):
    """GET /login should return HTML login page."""
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")


def test_seeded_user_can_login(client):
    """The seeded testuser should be able to login."""
    resp = client.post("/login", data={
        "username": "testuser",
        "password": "testpass"
    })
    assert resp.status_code == 200


def test_register_sets_session_cookie(client):
    """POST /register should set session cookie (auto-login)."""
    resp = client.post("/register", json={
        "username": "eve",
        "password": "Secret123"
    })
    assert resp.status_code == 201
    assert "Set-Cookie" in resp.headers
    assert "session_token" in resp.headers["Set-Cookie"]


def test_register_then_access_authorize(client):
    """After registration, the session cookie should allow accessing /authorize."""
    resp = client.post("/register", json={
        "username": "frank",
        "password": "Secret123"
    })
    assert resp.status_code == 201
    cookie = resp.headers["Set-Cookie"]

    # Use the cookie to access /authorize (should get consent page, not login redirect)
    params = {
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "code_challenge": "test-challenge",
        "code_challenge_method": "S256",
        "state": "test-state",
    }
    resp = client.get("/authorize", params=params, headers={"Cookie": cookie})
    assert resp.status_code == 200
    assert "Authorization Request" in resp.text
