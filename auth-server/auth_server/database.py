"""SQLite database initialization and access."""
import sqlite3
import uuid
import json
from datetime import datetime, timezone

import bcrypt

import auth_server.config as _config


def get_db() -> sqlite3.Connection:
    """Get a database connection with row factory enabled."""
    conn = sqlite3.connect(_config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------------------
# Sessions (persisted — survives restarts, enables horizontal scaling)
# ---------------------------------------------------------------------------

def create_session(session_token: str, user_id: str, username: str, expires_at: str) -> None:
    """Persist a login session row."""
    db = get_db()
    db.execute(
        "INSERT INTO sessions (session_token, user_id, username, expires_at) VALUES (?, ?, ?, ?)",
        (session_token, user_id, username, expires_at),
    )
    db.commit()
    db.close()


def get_session(session_token: str) -> dict | None:
    """Return a valid session dict, or None (deleting it when expired)."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM sessions WHERE session_token = ?", (session_token,),
    ).fetchone()
    if row is None:
        db.close()
        return None
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        db.execute("DELETE FROM sessions WHERE session_token = ?", (session_token,))
        db.commit()
        db.close()
        return None
    db.close()
    return dict(row)


def delete_session(session_token: str) -> None:
    """Remove a session row (logout)."""
    db = get_db()
    db.execute("DELETE FROM sessions WHERE session_token = ?", (session_token,))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Pending authorization (persisted with the session)
# ---------------------------------------------------------------------------

def set_pending_auth(session_token: str, data: dict) -> None:
    """Store the pending /authorize parameters for a session (upsert)."""
    db = get_db()
    db.execute(
        """INSERT INTO pending_auths
               (session_token, client_id, redirect_uri, code_challenge, scope, state)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(session_token) DO UPDATE SET
               client_id=excluded.client_id,
               redirect_uri=excluded.redirect_uri,
               code_challenge=excluded.code_challenge,
               scope=excluded.scope,
               state=excluded.state""",
        (
            session_token,
            data["client_id"],
            data["redirect_uri"],
            data["code_challenge"],
            data.get("scope", ""),
            data["state"],
        ),
    )
    db.commit()
    db.close()


def get_pending_auth(session_token: str) -> dict | None:
    """Return the pending /authorize parameters for a session."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM pending_auths WHERE session_token = ?", (session_token,),
    ).fetchone()
    db.close()
    return dict(row) if row else None


def clear_pending_auth(session_token: str) -> None:
    """Remove the pending /authorize parameters for a session."""
    db = get_db()
    db.execute("DELETE FROM pending_auths WHERE session_token = ?", (session_token,))
    db.commit()
    db.close()


def init_db() -> None:
    """Initialize database schema and seed data."""
    conn = sqlite3.connect(_config.DB_PATH)
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
            role          TEXT NOT NULL DEFAULT 'user',
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

        CREATE TABLE IF NOT EXISTS user_llm_configs (
            user_id                    TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            provider_id                TEXT NOT NULL,
            service_name               TEXT NOT NULL,
            api_type                   TEXT NOT NULL,
            upstream_url               TEXT NOT NULL,
            upstream_api_key_encrypted TEXT NOT NULL DEFAULT '',
            upstream_auth_type         TEXT NOT NULL,
            default_model              TEXT NOT NULL,
            cheap_model                TEXT NOT NULL,
            models                     TEXT NOT NULL,
            max_output_tokens          INTEGER NOT NULL,
            config_version             INTEGER NOT NULL DEFAULT 1,
            created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at                 TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS sessions (
            session_token TEXT PRIMARY KEY,
            user_id       TEXT NOT NULL REFERENCES users(id),
            username      TEXT NOT NULL,
            expires_at    TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS pending_auths (
            session_token   TEXT PRIMARY KEY REFERENCES sessions(session_token) ON DELETE CASCADE,
            client_id       TEXT NOT NULL,
            redirect_uri    TEXT NOT NULL,
            code_challenge  TEXT NOT NULL,
            scope           TEXT,
            state           TEXT NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_auth_codes_expires ON authorization_codes(expires_at);
        CREATE INDEX IF NOT EXISTS idx_tokens_jti ON tokens(jti);
    """)

    # Add roles to databases created before administrator management existed.
    user_columns = {
        row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()
    }
    if "role" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")

    # Seed default client (only if not exists). The redirect_uri registration
    # uses a port wildcard ("http://127.0.0.1:*") which the exact matcher
    # interprets as "any port on this loopback host".
    cursor.execute("SELECT COUNT(*) FROM clients WHERE id = ?", ("flavor-code-cli",))
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO clients (id, name, redirect_uris) VALUES (?, ?, ?)",
            ("flavor-code-cli", "flavor-code CLI",
             json.dumps(["http://127.0.0.1:*"]))
        )
    else:
        # Migrate databases seeded with the old prefix value.
        cursor.execute(
            "UPDATE clients SET redirect_uris = ? WHERE id = 'flavor-code-cli' AND redirect_uris = ?",
            (json.dumps(["http://127.0.0.1:*"]), json.dumps(["http://127.0.0.1:"])),
        )

    # Seed test user (only if not exists)
    cursor.execute("SELECT COUNT(*) FROM users WHERE username = ?", ("testuser",))
    if cursor.fetchone()[0] == 0:
        password_hash = bcrypt.hashpw(b"testpass", bcrypt.gensalt()).decode()
        cursor.execute(
            "INSERT INTO users (id, username, password_hash) VALUES (?, ?, ?)",
            (str(uuid.uuid4()), "testuser", password_hash)
        )

    # Seed (and intentionally rotate) the configured administrator password.
    # No administrator is created when the deployment omits either value.
    if _config.ADMIN_USERNAME and _config.ADMIN_PASSWORD:
        admin_hash = bcrypt.hashpw(
            _config.ADMIN_PASSWORD.encode(), bcrypt.gensalt(),
        ).decode()
        existing_admin = cursor.execute(
            "SELECT id FROM users WHERE username = ?", (_config.ADMIN_USERNAME,),
        ).fetchone()
        if existing_admin:
            cursor.execute(
                "UPDATE users SET password_hash = ?, role = 'admin' WHERE id = ?",
                (admin_hash, existing_admin[0]),
            )
        else:
            cursor.execute(
                "INSERT INTO users (id, username, password_hash, role) VALUES (?, ?, ?, 'admin')",
                (str(uuid.uuid4()), _config.ADMIN_USERNAME, admin_hash),
            )

    conn.commit()
    conn.close()

    # Preserve the original single-upstream deployment as a migration template
    # for the built-in account. This does not apply to newly registered users.
    from auth_server.llm_config import get_llm_config, save_llm_config
    conn = get_db()
    seeded = conn.execute(
        "SELECT id FROM users WHERE username = ?", ("testuser",),
    ).fetchone()
    conn.close()
    if seeded and get_llm_config(seeded["id"]) is None:
        save_llm_config(seeded["id"], {
            "provider_id": _config.DEFAULT_LLM_PROVIDER_ID,
            "service_name": _config.DEFAULT_LLM_SERVICE_NAME,
            "api_type": _config.DEFAULT_LLM_API_TYPE,
            "upstream_url": _config.DEFAULT_LLM_UPSTREAM_URL,
            "upstream_api_key": _config.DEFAULT_LLM_UPSTREAM_API_KEY,
            "upstream_auth_type": _config.DEFAULT_LLM_UPSTREAM_AUTH_TYPE,
            "default_model": _config.DEFAULT_LLM_MODEL,
            "cheap_model": _config.DEFAULT_LLM_CHEAP_MODEL,
            "models": list(dict.fromkeys([
                _config.DEFAULT_LLM_MODEL, _config.DEFAULT_LLM_CHEAP_MODEL,
            ])),
            "max_output_tokens": _config.DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        })
