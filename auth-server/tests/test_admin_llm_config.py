"""Administrator management of per-user LLM routes."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import auth_server.config as config
from auth_server.database import init_db


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="admin_llm_test_")
    os.close(fd)
    previous = config.DB_PATH
    config.DB_PATH = path
    config.SEED_TEST_USER = True
    monkeypatch.setattr(config, "ADMIN_USERNAME", "route-admin", raising=False)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "admin-test-password", raising=False)
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


def login(client, username, password):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 200


def route_payload(**overrides):
    value = {
        "provider_id": "deepseek",
        "service_name": "Managed DeepSeek",
        "api_type": "anthropic",
        "upstream_url": "https://llm.example.test/anthropic",
        "upstream_api_key": "managed-upstream-secret",
        "upstream_auth_type": "x-api-key",
        "default_model": "deepseek-v4-pro",
        "cheap_model": "deepseek-v4-flash",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "max_output_tokens": 65536,
    }
    value.update(overrides)
    return value


def test_seeded_admin_can_login_and_me_reports_role(client):
    login(client, "route-admin", "admin-test-password")
    response = client.get("/api/me")
    assert response.status_code == 200
    assert response.json()["username"] == "route-admin"
    assert response.json()["role"] == "admin"


def test_regular_user_cannot_access_admin_apis(client):
    login(client, "testuser", "testpass")
    me = client.get("/api/me").json()
    assert me["role"] == "user"
    assert client.get("/api/admin/users").status_code == 403
    assert client.put(
        f"/api/admin/users/{me['id']}/llm-config", json=route_payload(),
    ).status_code == 403


def test_admin_lists_users_with_upstream_key_for_management(client):
    """Admins manage real credentials, so the roster returns decrypted keys.

    Access is still strictly limited to the admin role (see the 403 test
    above for regular users).
    """
    login(client, "testuser", "testpass")
    saved = client.put("/api/me/llm-config", json=route_payload())
    assert saved.status_code == 200

    client.cookies.clear()
    login(client, "route-admin", "admin-test-password")
    response = client.get("/api/admin/users")
    assert response.status_code == 200
    users = response.json()["users"]
    test_user = next(item for item in users if item["username"] == "testuser")
    assert test_user["role"] == "user"
    assert test_user["llm_config"]["api_key_configured"] is True
    assert test_user["llm_config"]["upstream_api_key"] == "managed-upstream-secret"


def test_admin_updates_another_user_and_preserves_blank_key(client):
    login(client, "testuser", "testpass")
    first = client.put("/api/me/llm-config", json=route_payload()).json()
    user_id = client.get("/api/me").json()["id"]

    client.cookies.clear()
    login(client, "route-admin", "admin-test-password")
    update = route_payload(service_name="Operations LLM")
    update.pop("upstream_api_key")
    response = client.put(f"/api/admin/users/{user_id}/llm-config", json=update)
    assert response.status_code == 200
    assert response.json()["service_name"] == "Operations LLM"
    assert response.json()["api_key_configured"] is True
    assert response.json()["config_version"] == first["config_version"] + 1


def test_admin_update_rejects_unknown_user(client):
    login(client, "route-admin", "admin-test-password")
    response = client.put("/api/admin/users/missing/llm-config", json=route_payload())
    assert response.status_code == 404

