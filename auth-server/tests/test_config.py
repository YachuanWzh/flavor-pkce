"""Startup safety: insecure default secrets fail fast; testuser seeding is
opt-in (P0-8)."""
import os
import tempfile

import pytest

import auth_server.config as config


@pytest.fixture(autouse=True)
def restore_config(monkeypatch):
    """Restore original module attributes after each test."""
    monkeypatch.setattr(config, "ALLOW_INSECURE_DEFAULTS", False)
    monkeypatch.setattr(config, "SECRET_KEY", "dev-secret-change-in-production")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "dev-internal-token-change-me")
    monkeypatch.setattr(config, "SEED_TEST_USER", False)
    yield


def test_validate_rejects_default_secret_key():
    from auth_server.config import validate_production_config
    with pytest.raises(RuntimeError):
        validate_production_config()


def test_validate_rejects_default_internal_token(monkeypatch):
    from auth_server.config import validate_production_config
    monkeypatch.setattr(config, "SECRET_KEY", "a-strong-random-value-1")
    with pytest.raises(RuntimeError):
        validate_production_config()


def test_validate_passes_with_strong_values(monkeypatch):
    from auth_server.config import validate_production_config
    monkeypatch.setattr(config, "SECRET_KEY", "a-strong-random-value-1")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "another-strong-value-2")
    validate_production_config()  # should not raise


def test_validate_passes_when_insecure_defaults_allowed(monkeypatch):
    from auth_server.config import validate_production_config
    monkeypatch.setattr(config, "ALLOW_INSECURE_DEFAULTS", True)
    validate_production_config()  # should not raise


def test_init_db_does_not_seed_testuser_by_default():
    from auth_server.database import init_db, get_db

    fd, tmp_path = tempfile.mkstemp(suffix=".db", prefix="cfg_test_")
    os.close(fd)
    old_path = config.DB_PATH
    config.DB_PATH = tmp_path
    try:
        init_db()
        db = get_db()
        row = db.execute(
            "SELECT COUNT(*) FROM users WHERE username = 'testuser'"
        ).fetchone()
        db.close()
        assert row[0] == 0
    finally:
        config.DB_PATH = old_path
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except PermissionError:
                pass
