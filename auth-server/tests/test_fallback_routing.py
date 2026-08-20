"""Fallback profile linkage and internal routing payload for gateway failover."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import auth_server.config as config
from auth_server.database import get_db, init_db


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="fallback_routing_test_")
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


INTERNAL_HEADERS = {"x-internal-service-token": config.INTERNAL_SERVICE_TOKEN}


def login_testuser(client):
    response = client.post(
        "/login", data={"username": "testuser", "password": "testpass"},
    )
    assert response.status_code == 200


def config_payload(**overrides):
    value = {
        "provider_id": "deepseek",
        "service_name": "Primary DeepSeek",
        "api_type": "anthropic",
        "upstream_url": "https://primary.example.test",
        "upstream_api_key": "primary-key",
        "upstream_auth_type": "x-api-key",
        "default_model": "deepseek-v4-pro",
        "cheap_model": "deepseek-v4-flash",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "max_output_tokens": 65536,
    }
    value.update(overrides)
    return value


def make_profile(client, name, **overrides):
    payload = config_payload(**overrides)
    payload["name"] = name
    payload["upstream_api_key"] = overrides.get("upstream_api_key", "fallback-key")
    response = client.post("/api/me/llm-config-profiles", json=payload)
    assert response.status_code == 201
    return response.json()


def test_set_and_clear_fallback_profile(client):
    login_testuser(client)
    client.put("/api/me/llm-config", json=config_payload())
    profile = make_profile(
        client, "Backup",
        service_name="Backup Route",
        upstream_url="https://backup.example.test",
    )

    response = client.put(
        "/api/me/llm-config/fallback",
        json={"fallback_profile_id": profile["id"]},
    )
    assert response.status_code == 200
    assert response.json()["fallback_profile_id"] == profile["id"]

    cleared = client.put("/api/me/llm-config/fallback", json={"fallback_profile_id": None})
    assert cleared.status_code == 200
    assert cleared.json()["fallback_profile_id"] is None


def test_fallback_unknown_or_foreign_profile_rejected(client):
    login_testuser(client)
    client.put("/api/me/llm-config", json=config_payload())
    response = client.put(
        "/api/me/llm-config/fallback",
        json={"fallback_profile_id": "missing"},
    )
    assert response.status_code == 404


def test_fallback_requires_active_config(client):
    # A freshly registered user has no active LLM configuration yet.
    client.post(
        "/register", json={"username": "newbie", "password": "Passw0rd-strong"},
    )
    client.post(
        "/login", data={"username": "newbie", "password": "Passw0rd-strong"},
    )
    profile = make_profile(client, "Backup")
    response = client.put(
        "/api/me/llm-config/fallback",
        json={"fallback_profile_id": profile["id"]},
    )
    assert response.status_code == 404


def test_internal_api_includes_fallback_route(client):
    login_testuser(client)
    saved = client.put("/api/me/llm-config", json=config_payload()).json()
    profile = make_profile(
        client, "Backup",
        service_name="Backup Route",
        upstream_url="https://backup.example.test",
        upstream_api_key="fallback-key",
    )
    client.put(
        "/api/me/llm-config/fallback",
        json={"fallback_profile_id": profile["id"]},
    )

    user_id = client.get("/api/me").json()["id"]
    response = client.get(
        f"/internal/users/{user_id}/llm-config",
        params={"version": saved["config_version"]},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["service_name"] == "Primary DeepSeek"
    fallback = body["fallback"]
    assert fallback is not None
    assert fallback["service_name"] == "Backup Route"
    assert fallback["upstream_url"] == "https://backup.example.test"
    assert fallback["upstream_api_key"] == "fallback-key"
    assert fallback["api_type"] == "anthropic"
    assert fallback["models"] == ["deepseek-v4-pro", "deepseek-v4-flash"]


def test_internal_api_fallback_is_null_when_unset(client):
    login_testuser(client)
    saved = client.put("/api/me/llm-config", json=config_payload()).json()
    user_id = client.get("/api/me").json()["id"]
    response = client.get(
        f"/internal/users/{user_id}/llm-config",
        params={"version": saved["config_version"]},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["fallback"] is None


def test_fallback_setting_does_not_bump_config_version(client):
    """Choosing a fallback is a routing preference, not a route change.

    Old JWTs keep working; the gateway picks the fallback only on failure.
    """
    login_testuser(client)
    saved = client.put("/api/me/llm-config", json=config_payload()).json()
    profile = make_profile(client, "Backup")
    client.put(
        "/api/me/llm-config/fallback",
        json={"fallback_profile_id": profile["id"]},
    )
    current = client.get("/api/me/llm-config").json()
    assert current["config_version"] == saved["config_version"]


def test_fallback_key_stays_within_its_profile(client):
    """The owner may read their own profile keys, but the active-config
    response must never blend in the fallback profile's key."""
    login_testuser(client)
    client.put("/api/me/llm-config", json=config_payload())
    profile = make_profile(client, "Backup", upstream_api_key="super-secret-fallback")
    client.put(
        "/api/me/llm-config/fallback",
        json={"fallback_profile_id": profile["id"]},
    )
    me_config = client.get("/api/me/llm-config").json()
    assert me_config["upstream_api_key"] == "primary-key"
    assert "super-secret-fallback" not in str(me_config)
    # The profile's own listing does expose it to its owner (by design).
    profiles = client.get("/api/me/llm-config-profiles").json()["profiles"]
    assert profiles[0]["upstream_api_key"] == "super-secret-fallback"


def test_admin_view_of_other_user_exposes_key_for_management(client):
    """Admins are trusted with upstream credentials (rotate/provision).

    The roster and edit responses include decrypted keys; regular users can
    never read another user's key (see test_other_user_profile_key_not_visible).
    """
    login_testuser(client)
    client.put("/api/me/llm-config", json=config_payload())
    user_id = client.get("/api/me").json()["id"]
    profile = make_profile(client, "Backup", upstream_api_key="super-secret-fallback")
    client.put(
        "/api/me/llm-config/fallback",
        json={"fallback_profile_id": profile["id"]},
    )

    client.cookies.clear()
    client.post(
        "/register", json={"username": "boss", "password": "Passw0rd-strong"},
    )
    client.post("/login", data={"username": "boss", "password": "Passw0rd-strong"})
    db = get_db()
    db.execute("UPDATE users SET role = 'admin' WHERE username = 'boss'")
    db.commit()
    db.close()

    roster = client.get("/api/admin/users")
    assert roster.status_code == 200
    target = next(u for u in roster.json()["users"] if u["id"] == user_id)
    assert target["llm_config"]["upstream_api_key"] == "primary-key"

    update = {k: v for k, v in config_payload().items() if k != "upstream_api_key"}
    update["service_name"] = "Admin Edit"
    edited = client.put(f"/api/admin/users/{user_id}/llm-config", json=update)
    assert edited.status_code == 200
    assert edited.json()["upstream_api_key"] == "primary-key"
