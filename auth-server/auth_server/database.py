"""SQLite database initialization and access."""
import sqlite3
import uuid
import json
from datetime import datetime, timezone

import bcrypt

from auth_server.config import DB_PATH


def get_db() -> sqlite3.Connection:
    """Get a database connection with row factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Initialize database schema and seed data."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            id            TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            redirect_uris TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS authorization_codes (
            code             TEXT PRIMARY KEY,
            client_id        TEXT NOT NULL REFERENCES clients(id),
            redirect_uri     TEXT NOT NULL,
            code_challenge   TEXT NOT NULL,
            user_id          TEXT NOT NULL REFERENCES users(id),
            scope            TEXT,
            expires_at       TEXT NOT NULL,
            used             INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tokens (
            id              TEXT PRIMARY KEY,
            jti             TEXT NOT NULL UNIQUE,
            client_id       TEXT NOT NULL REFERENCES clients(id),
            user_id         TEXT NOT NULL REFERENCES users(id),
            scope           TEXT,
            expires_at      TEXT NOT NULL,
            revoked         INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_auth_codes_expires ON authorization_codes(expires_at);
        CREATE INDEX IF NOT EXISTS idx_tokens_jti ON tokens(jti);
    """)

    # Seed default client (only if not exists)
    cursor.execute("SELECT COUNT(*) FROM clients WHERE id = ?", ("flavor-code-cli",))
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO clients (id, name, redirect_uris) VALUES (?, ?, ?)",
            ("flavor-code-cli", "flavor-code CLI",
             json.dumps(["http://127.0.0.1:"]))
        )

    # Seed test user (only if not exists)
    cursor.execute("SELECT COUNT(*) FROM users WHERE username = ?", ("testuser",))
    if cursor.fetchone()[0] == 0:
        password_hash = bcrypt.hashpw(b"testpass", bcrypt.gensalt()).decode()
        cursor.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), "testuser", password_hash)
        )

    conn.commit()
    conn.close()
