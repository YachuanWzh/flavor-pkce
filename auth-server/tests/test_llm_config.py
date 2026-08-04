"""Per-user LLM configuration API and storage tests."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import auth_server.config as config
from auth_server.database import get_db, init_db


@pytest.fixture(autouse=True)
def isolated_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="llm_config_test_")
    os.close(fd)
    previous = config.DB_PATH
    config.DB_PATH = path
    config.SEED_TEST_USER = True
    init_db()
    yield
    config.DB_PATH = previous
    try:
        os.remove(path)
    except PermissionError:
        pass


@pytest.fixture
def client():
    from auth_server.main import app
    return TestClient(app)


def login(client, username="testuser", password="testpass"):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 200


def sample_config(**overrides):
    value = {
        "provider_id": "deepseek",
        "service_name": "Enterprise DeepSeek",
        "api_type": "anthropic",
        "upstream_url": "https://llm.example.test/anthropic",
        "upstream_api_key": "upstream-secret-value",
        "upstream_auth_type": "x-api-key",
        "default_model": "deepseek-v4-pro",
        "cheap_model": "deepseek-v4-flash",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "max_output_tokens": 65536,
    }
    value.update(overrides)
    return value


def test_user_can_save_and_read_public_config_without_secret(client):
    login(client)
    initial_version = client.get("/api/me/llm-config").json()["config_version"]
    saved = client.put("/api/me/llm-config", json=sample_config())
    assert saved.status_code == 200
    assert saved.json()["config_version"] == initial_version + 1

    response = client.get("/api/me/llm-config")
    assert response.status_code == 200
    body = response.json()
    assert body["service_name"] == "Enterprise DeepSeek"
    assert body["api_key_configured"] is True
    assert "upstream_api_key" not in body
    assert "upstream-secret-value" not in response.text

    db = get_db()
    row = db.execute("SELECT upstream_api_key_encrypted FROM user_llm_configs").fetchone()
    db.close()
    assert row is not None
    assert "upstream-secret-value" not in row["upstream_api_key_encrypted"]


def test_update_preserves_key_and_increments_version(client):
    login(client)
    first = client.put("/api/me/llm-config", json=sample_config())
    assert first.status_code == 200
    updated = sample_config(service_name="Renamed", default_model="deepseek-v5")
    updated.pop("upstream_api_key")
    updated["models"] = ["deepseek-v5", "deepseek-v4-flash"]
    response = client.put("/api/me/llm-config", json=updated)
    assert response.status_code == 200
    assert response.json()["config_version"] == first.json()["config_version"] + 1
    assert response.json()["api_key_configured"] is True


def test_configs_are_isolated_by_user(client):
    login(client)
    client.put("/api/me/llm-config", json=sample_config(service_name="Test User LLM"))

    client.post("/register", json={"username": "alice", "password": "AlicePass1"})
    client.put("/api/me/llm-config", json=sample_config(
        provider_id="qwen", service_name="Alice LLM",
        default_model="qwen3-coder", cheap_model="qwen3-coder-fast",
        models=["qwen3-coder", "qwen3-coder-fast"],
    ))
    assert client.get("/api/me/llm-config").json()["service_name"] == "Alice LLM"

    client.cookies.clear()
    login(client)
    assert client.get("/api/me/llm-config").json()["service_name"] == "Test User LLM"


def test_internal_config_requires_service_token_and_checks_version(client, monkeypatch):
    login(client)
    saved = client.put("/api/me/llm-config", json=sample_config()).json()
    me = client.get("/api/me").json()
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "internal-test-token")

    denied = client.get(f"/internal/users/{me['id']}/llm-config", params={"version": 1})
    assert denied.status_code == 401
    headers = {"X-Internal-Service-Token": "internal-test-token"}
    ok = client.get(f"/internal/users/{me['id']}/llm-config", params={"version": saved["config_version"]}, headers=headers)
    assert ok.status_code == 200
    assert ok.json()["upstream_api_key"] == "upstream-secret-value"
    stale = client.get(f"/internal/users/{me['id']}/llm-config", params={"version": 999}, headers=headers)
    assert stale.status_code == 409


@pytest.mark.parametrize("field,value", [
    ("provider_id", "bad provider"),
    ("api_type", "unknown"),
    ("upstream_url", "file:///etc/passwd"),
    ("upstream_auth_type", "basic"),
    ("models", []),
    ("max_output_tokens", 0),
])
def test_invalid_configuration_is_rejected(client, field, value):
    login(client)
    response = client.put("/api/me/llm-config", json=sample_config(**{field: value}))
    assert response.status_code == 400
