"""Test database schema and operations."""
import os
import tempfile
import pytest
import sqlite3
from pathlib import Path

from auth_server.database import get_db, init_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    """Use a temporary database for each test."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="db_test_")
    os.close(fd)
    config.DB_PATH = tmp_path
    yield
    config.DB_PATH = _original_db_path
    if os.path.exists(tmp_path):
        os.remove(tmp_path)


def test_init_db_creates_tables():
    """Database initialization should create all required tables."""
    init_db()

    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    # Check tables exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    expected_tables = [
        "authorization_codes",
        "clients",
        "tokens",
        "users",
    ]
    for table in expected_tables:
        assert table in tables, f"Table '{table}' should exist"

    conn.close()


def test_clients_schema():
    """Clients table should have correct columns."""
    init_db()
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(clients)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}

    assert "id" in columns
    assert "name" in columns
    assert "redirect_uris" in columns
    assert "created_at" in columns
    conn.close()


def test_users_schema():
    """Users table should have correct columns."""
    init_db()
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(users)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}

    assert "id" in columns
    assert "username" in columns
    assert "password_hash" in columns
    assert "role" in columns
    assert "created_at" in columns
    conn.close()


def test_init_db_migrates_existing_users_to_regular_role():
    """Older databases gain the role column without losing existing users."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("""
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
        ("legacy-id", "legacy-user", "legacy-hash"),
    )
    conn.commit()
    conn.close()

    init_db()

    conn = sqlite3.connect(config.DB_PATH)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    role = conn.execute(
        "SELECT role FROM users WHERE id = ?", ("legacy-id",),
    ).fetchone()[0]
    conn.close()
    assert "role" in columns
    assert role == "user"


def test_authorization_codes_schema():
    """Authorization codes table should have correct columns with foreign keys."""
    init_db()
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(authorization_codes)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}

    assert "code" in columns
    assert "client_id" in columns
    assert "redirect_uri" in columns
    assert "code_challenge" in columns
    assert "user_id" in columns
    assert "scope" in columns
    assert "expires_at" in columns
    assert "used" in columns
    assert "created_at" in columns
    conn.close()


def test_tokens_schema():
    """Tokens table should have correct columns."""
    init_db()
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("PRAGMA table_info(tokens)")
    columns = {row[1]: row[2] for row in cursor.fetchall()}

    assert "id" in columns
    assert "jti" in columns
    assert "client_id" in columns
    assert "user_id" in columns
    assert "scope" in columns
    assert "expires_at" in columns
    assert "revoked" in columns
    assert "created_at" in columns
    conn.close()


def test_seed_data_creates_default_client(monkeypatch):
    """Init should seed a default client and test user."""
    monkeypatch.setattr(config, "SEED_TEST_USER", True)
    init_db()
    conn = sqlite3.connect(config.DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT id, name, redirect_uris FROM clients WHERE id = ?",
                   ("flavor-code-cli",))
    client = cursor.fetchone()
    assert client is not None
    assert client[0] == "flavor-code-cli"
    assert client[1] == "flavor-code CLI"
    assert "http://127.0.0.1:" in client[2]

    cursor.execute("SELECT id, name, redirect_uris FROM clients WHERE id = ?",
                   ("flavor-lite-cli",))
    lite_client = cursor.fetchone()
    assert lite_client is not None
    assert lite_client[0] == "flavor-lite-cli"
    assert lite_client[1] == "flavor-lite CLI"
    assert "http://127.0.0.1:" in lite_client[2]

    cursor.execute("SELECT id, username FROM users WHERE username = ?",
                   ("testuser",))
    user = cursor.fetchone()
    assert user is not None
    assert user[1] == "testuser"

    conn.close()
