"""Test user registration and login."""
import os
import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db, DB_PATH
import auth_server.config as config


@pytest.fixture(autouse=True)
def clean_db():
    """Remove test database before each test."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    # Override test DB path
    config.DB_PATH = DB_PATH
    init_db()
    yield
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)


@pytest.fixture
def client():
    """Create test client."""
    from auth_server.main import app
    return TestClient(app)


def test_register_new_user(client):
    """POST /register should create a new user."""
    resp = client.post("/register", json={
        "username": "alice",
        "password": "secret123"
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
        "password": "secret123"
    })
    resp = client.post("/register", json={
        "username": "alice",
        "password": "another456"
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
        "password": "mypassword"
    })
    resp = client.post("/login", data={
        "username": "charlie",
        "password": "mypassword"
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
        "password": "correct"
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
