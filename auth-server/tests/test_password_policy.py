"""Test the password strength policy enforced at registration (P0-9)."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="pw_test_")
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


def test_register_rejects_too_short_password(client):
    resp = client.post("/register", json={"username": "alice", "password": "Short1!"})
    assert resp.status_code == 400


def test_register_rejects_no_uppercase(client):
    resp = client.post("/register", json={"username": "alice", "password": "secret123"})
    assert resp.status_code == 400


def test_register_rejects_no_digit(client):
    resp = client.post("/register", json={"username": "alice", "password": "Secretpass"})
    assert resp.status_code == 400


def test_register_rejects_no_lowercase(client):
    resp = client.post("/register", json={"username": "alice", "password": "SECRET123"})
    assert resp.status_code == 400


def test_register_accepts_policy_compliant_password(client):
    resp = client.post("/register", json={"username": "alice", "password": "Secret123"})
    assert resp.status_code == 201


def test_register_accepts_minimum_length_compliant(client):
    # 8 chars, upper + lower + digit
    resp = client.post("/register", json={"username": "bob", "password": "Abcdef12"})
    assert resp.status_code == 201


def test_register_rejects_weak_password_even_for_long(client):
    # Long but no digits: rejected
    resp = client.post("/register", json={"username": "carol", "password": "AAAAAAAAaaaa"})
    assert resp.status_code == 400
