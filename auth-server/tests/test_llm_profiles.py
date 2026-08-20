"""Per-user named LLM configuration profiles (save once, reuse via dropdown)."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import auth_server.config as config
from auth_server.database import init_db


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="llm_profiles_test_")
    os.close(fd)
    previous = config.DB_PATH
    config.DB_PATH = path
    config.SEED_TEST_USER = True
    monkeypatch.setattr(config, "ADMIN_USERNAME", "", raising=False)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "", raising=False)
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


def login_testuser(client):
    response = client.post(
        "/login", data={"username": "testuser", "password": "testpass"},
    )
    assert response.status_code == 200


def profile_payload(name="DeepSeek Prod", **overrides):
    value = {
        "name": name,
        "provider_id": "deepseek",
        "service_name": "Enterprise DeepSeek",
        "api_type": "anthropic",
        "upstream_url": "https://llm.example.test/anthropic",
        "upstream_api_key": "profile-upstream-secret",
        "upstream_auth_type": "x-api-key",
        "default_model": "deepseek-v4-pro",
        "cheap_model": "deepseek-v4-flash",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "max_output_tokens": 65536,
    }
    value.update(overrides)
    return value


def test_requires_authentication(client):
    assert client.get("/api/me/llm-config-profiles").status_code == 401
    assert client.post(
        "/api/me/llm-config-profiles", json=profile_payload(),
    ).status_code == 401


def test_create_and_list_profile_returns_owner_key(client):
    """The owner's own session reads the decrypted key back for display."""
    login_testuser(client)
    created = client.post("/api/me/llm-config-profiles", json=profile_payload())
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "DeepSeek Prod"
    assert body["api_key_configured"] is True
    assert body["upstream_api_key"] == "profile-upstream-secret"

    listing = client.get("/api/me/llm-config-profiles")
    assert listing.status_code == 200
    profiles = listing.json()["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["id"] == body["id"]
    assert profiles[0]["upstream_api_key"] == "profile-upstream-secret"


def test_duplicate_name_rejected(client):
    login_testuser(client)
    assert client.post(
        "/api/me/llm-config-profiles", json=profile_payload(),
    ).status_code == 201
    response = client.post("/api/me/llm-config-profiles", json=profile_payload())
    assert response.status_code == 409


def test_invalid_profile_rejected(client):
    login_testuser(client)
    response = client.post(
        "/api/me/llm-config-profiles",
        json=profile_payload(default_model="not-in-list"),
    )
    assert response.status_code == 400


def test_update_profile_preserves_key_when_omitted(client):
    login_testuser(client)
    created = client.post(
        "/api/me/llm-config-profiles", json=profile_payload(),
    ).json()
    update = profile_payload(service_name="Renamed Service")
    update.pop("upstream_api_key")
    response = client.put(
        f"/api/me/llm-config-profiles/{created['id']}", json=update,
    )
    assert response.status_code == 200
    assert response.json()["service_name"] == "Renamed Service"
    assert response.json()["api_key_configured"] is True


def test_delete_profile(client):
    login_testuser(client)
    created = client.post(
        "/api/me/llm-config-profiles", json=profile_payload(),
    ).json()
    response = client.delete(f"/api/me/llm-config-profiles/{created['id']}")
    assert response.status_code == 200
    assert client.get("/api/me/llm-config-profiles").json()["profiles"] == []


def test_activate_profile_copies_to_active_config_and_bumps_version(client):
    login_testuser(client)
    # Seed an active config (version 1).
    active = client.put(
        "/api/me/llm-config",
        json={k: v for k, v in profile_payload().items() if k != "name"},
    ).json()
    created = client.post(
        "/api/me/llm-config-profiles",
        json=profile_payload(
            name="Backup Route",
            service_name="Backup DeepSeek",
            default_model="deepseek-v4-flash",
            cheap_model="deepseek-v4-flash",
        ),
    ).json()

    response = client.post(
        f"/api/me/llm-config-profiles/{created['id']}/activate",
    )
    assert response.status_code == 200
    assert response.json()["config_version"] == active["config_version"] + 1

    current = client.get("/api/me/llm-config").json()
    assert current["service_name"] == "Backup DeepSeek"
    assert current["default_model"] == "deepseek-v4-flash"
    assert current["active_profile_id"] == created["id"]


def test_activate_missing_profile_404(client):
    login_testuser(client)
    response = client.post("/api/me/llm-config-profiles/missing/activate")
    assert response.status_code == 404


def test_own_profile_returns_saved_key_in_plaintext(client):
    """The owner's session may read back the saved key for display/editing."""
    login_testuser(client)
    created = client.post(
        "/api/me/llm-config-profiles", json=profile_payload(),
    ).json()

    listing = client.get("/api/me/llm-config-profiles").json()
    assert listing["profiles"][0]["upstream_api_key"] == "profile-upstream-secret"

    fetched = client.get(f"/api/me/llm-config-profiles/{created['id']}").json()
    assert fetched["upstream_api_key"] == "profile-upstream-secret"


def test_own_active_config_returns_saved_key_in_plaintext(client):
    login_testuser(client)
    active = client.put(
        "/api/me/llm-config",
        json={k: v for k, v in profile_payload().items() if k != "name"},
    ).json()
    assert active["upstream_api_key"] == "profile-upstream-secret"
    fetched = client.get("/api/me/llm-config").json()
    assert fetched["upstream_api_key"] == "profile-upstream-secret"


def test_activate_profile_returns_key(client):
    login_testuser(client)
    created = client.post(
        "/api/me/llm-config-profiles", json=profile_payload(name="K"),
    ).json()
    activated = client.post(
        f"/api/me/llm-config-profiles/{created['id']}/activate",
    ).json()
    assert activated["upstream_api_key"] == "profile-upstream-secret"


def test_other_user_profile_key_not_visible(client):
    login_testuser(client)
    created = client.post(
        "/api/me/llm-config-profiles", json=profile_payload(),
    ).json()
    client.cookies.clear()
    client.post(
        "/register", json={"username": "spy", "password": "Passw0rd-strong"},
    )
    client.post("/login", data={"username": "spy", "password": "Passw0rd-strong"})
    listing = client.get("/api/me/llm-config-profiles").json()
    assert listing["profiles"] == []
    foreign = client.get(f"/api/me/llm-config-profiles/{created['id']}")
    assert foreign.status_code == 404
    assert "profile-upstream-secret" not in foreign.text


def test_profiles_are_isolated_between_users(client):
    login_testuser(client)
    created = client.post(
        "/api/me/llm-config-profiles", json=profile_payload(),
    ).json()
    client.cookies.clear()

    # Register a second user and confirm they cannot see or activate it.
    client.post(
        "/register", json={"username": "other", "password": "Passw0rd-strong"},
    )
    client.post(
        "/login", data={"username": "other", "password": "Passw0rd-strong"},
    )
    assert client.get("/api/me/llm-config-profiles").json()["profiles"] == []
    assert client.post(
        f"/api/me/llm-config-profiles/{created['id']}/activate",
    ).status_code == 404
    assert client.delete(
        f"/api/me/llm-config-profiles/{created['id']}",
    ).status_code == 404
