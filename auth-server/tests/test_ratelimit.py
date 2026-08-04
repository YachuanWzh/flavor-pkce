"""Rate limiting and account lockout tests (P0-4)."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from auth_server.database import init_db
import auth_server.config as config

_original_db_path = config.DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="rl_test_")
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


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------

def test_rate_limit_window_respects_limit():
    from auth_server.ratelimit import is_rate_limited, record_attempt

    now = 1_000_000.0
    key = "k"
    for _ in range(3):
        record_attempt(key, window_seconds=60, now=now)
    # 3 attempts allowed with limit=3 (hits == limit is OK); 4th exceeds
    record_attempt(key, window_seconds=60, now=now)
    assert is_rate_limited(key, limit=3, window_seconds=60, now=now) is True
    assert is_rate_limited(key, limit=4, window_seconds=60, now=now) is False


def test_rate_limit_resets_on_new_window():
    from auth_server.ratelimit import is_rate_limited, record_attempt

    key = "k"
    for _ in range(3):
        record_attempt(key, window_seconds=60, now=1_000_000.0)
    # New window: bucket changes, count resets
    assert is_rate_limited(key, limit=3, window_seconds=60, now=1_000_060.0) is False


def test_failure_lock_after_max_failures():
    from auth_server.ratelimit import record_failure, is_locked

    key = "login:user:alice"
    now = 1_000_000.0
    for i in range(4):
        record_failure(key, max_failures=5, lock_seconds=300, now=now + i)
    assert is_locked(key, now=now + 4) is False
    record_failure(key, max_failures=5, lock_seconds=300, now=now + 5)
    assert is_locked(key, now=now + 5) is True


def test_lock_expires():
    from auth_server.ratelimit import record_failure, is_locked

    key = "login:user:alice"
    now = 1_000_000.0
    for i in range(5):
        record_failure(key, max_failures=5, lock_seconds=300, now=now + i)
    # 5th failure at now+4 sets locked_until = now+4+300
    assert is_locked(key, now=now + 5) is True
    assert is_locked(key, now=now + 305) is False


def test_reset_failures_unlocks():
    from auth_server.ratelimit import record_failure, is_locked, reset_failures

    key = "login:user:alice"
    now = 1_000_000.0
    for i in range(5):
        record_failure(key, max_failures=5, lock_seconds=300, now=now + i)
    reset_failures(key)
    assert is_locked(key, now=now + 5) is False


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------

def _register(client, username="alice", password="Secret123"):
    resp = client.post("/register", json={"username": username, "password": password})
    assert resp.status_code == 201


def test_login_locks_account_after_repeated_failures(client):
    _register(client)
    for _ in range(5):
        resp = client.post("/login", data={"username": "alice", "password": "WrongPass1"})
        assert resp.status_code == 401
    # 6th attempt with the CORRECT password must be rejected while locked
    resp = client.post("/login", data={"username": "alice", "password": "Secret123"})
    assert resp.status_code == 429
    assert "lock" in resp.text.lower() or "too many" in resp.text.lower()


def test_successful_login_resets_failure_count(client):
    _register(client)
    for _ in range(3):
        client.post("/login", data={"username": "alice", "password": "WrongPass1"})
    # Correct login resets the counter
    resp = client.post("/login", data={"username": "alice", "password": "Secret123"})
    assert resp.status_code == 200
    # After reset, 4 more failures do NOT lock yet (only 3 recorded after reset)
    for _ in range(3):
        client.post("/login", data={"username": "alice", "password": "WrongPass1"})
    resp = client.post("/login", data={"username": "alice", "password": "Secret123"})
    assert resp.status_code == 200


def test_register_rate_limited_per_ip(client):
    """Register endpoint is rate-limited by client IP."""
    import auth_server.config as cfg
    limit = cfg.REGISTER_RATE_LIMIT
    for i in range(limit):
        resp = client.post("/register", json={
            "username": f"user{i}", "password": "Secret123",
        })
        assert resp.status_code == 201
    # Next registration from same IP is throttled
    resp = client.post("/register", json={
        "username": "overflow", "password": "Secret123",
    })
    assert resp.status_code == 429


def test_token_endpoint_rate_limited(client):
    """The /token endpoint is rate-limited per IP."""
    import auth_server.config as cfg
    limit = cfg.TOKEN_RATE_LIMIT
    for _ in range(limit):
        client.post("/token", data={
            "grant_type": "authorization_code",
            "code": "x", "redirect_uri": "http://127.0.0.1:1/cb",
            "client_id": "flavor-code-cli", "code_verifier": "y",
        })
    resp = client.post("/token", data={
        "grant_type": "authorization_code",
        "code": "x", "redirect_uri": "http://127.0.0.1:1/cb",
        "client_id": "flavor-code-cli", "code_verifier": "y",
    })
    assert resp.status_code == 429
