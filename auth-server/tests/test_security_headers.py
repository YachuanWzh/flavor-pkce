"""Security response headers and secure cookies (P0-6)."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="hdr_test_")
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


def test_login_page_has_security_headers(client):
    resp = client.get("/login")
    assert resp.headers.get("X-Frame-Options") == "DENY"
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert "default-src 'self'" in resp.headers.get("Content-Security-Policy", "")


def test_authorize_page_has_security_headers(client):
    resp = client.get("/authorize", params={
        "response_type": "code",
        "client_id": "flavor-code-cli",
        "redirect_uri": "http://127.0.0.1:9999/callback",
        "code_challenge": "x",
        "code_challenge_method": "S256",
        "state": "s",
    })
    assert resp.headers.get("X-Frame-Options") == "DENY"


def test_api_response_has_security_headers(client):
    resp = client.post("/login", data={"username": "nobody", "password": "Wrong1"})
    assert resp.headers.get("X-Frame-Options") == "DENY"


def test_cookie_secure_when_configured(monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)
    from auth_server.main import app
    client = TestClient(app)
    resp = client.post("/register", json={"username": "alice", "password": "Secret123"})
    assert resp.status_code == 201
    cookie = resp.headers["Set-Cookie"]
    assert "session_token=" in cookie
    assert "Secure" in cookie


def test_cookie_not_secure_by_default(client):
    resp = client.post("/register", json={"username": "alice", "password": "Secret123"})
    assert resp.status_code == 201
    cookie = resp.headers["Set-Cookie"]
    assert "Secure" not in cookie


def test_hsts_header_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_HSTS", True)
    from auth_server.main import app
    client = TestClient(app)
    resp = client.get("/login")
    assert resp.headers.get("Strict-Transport-Security", "").startswith("max-age=31536000")


def test_no_hsts_by_default(client):
    resp = client.get("/login")
    assert "Strict-Transport-Security" not in resp.headers
