"""Administrator management of per-user LLM configuration profiles."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import auth_server.config as config
from auth_server.database import init_db


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="admin_profiles_test_")
    os.close(fd)
    previous = config.DB_PATH
    config.DB_PATH = path
    config.SEED_TEST_USER = True
    monkeypatch.setattr(config, "ADMIN_USERNAME", "fleet-admin", raising=False)
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


def profile_payload(name="Fleet Route", **overrides):
    value = {
        "name": name,
        "provider_id": "deepseek",
        "service_name": "Managed DeepSeek",
        "api_type": "anthropic",
        "upstream_url": "https://llm.example.test/anthropic",
        "upstream_api_key": "admin-provided-key",
        "upstream_auth_type": "x-api-key",
        "default_model": "deepseek-v4-pro",
        "cheap_model": "deepseek-v4-flash",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "max_output_tokens": 65536,
    }
    value.update(overrides)
    return value


def user_id_of(client, username):
    login(client, username, "testpass" if username == "testuser" else "Passw0rd-strong")
    user_id = client.get("/api/me").json()["id"]
    client.cookies.clear()
    return user_id


def test_regular_user_cannot_touch_admin_profile_apis(client):
    target = user_id_of(client, "testuser")
    login(client, "testuser", "testpass")
    base = f"/api/admin/users/{target}/llm-config-profiles"
    assert client.get(base).status_code == 403
    assert client.post(base, json=profile_payload()).status_code == 403
    assert client.post(f"{base}/x/activate").status_code == 403
    assert client.delete(f"{base}/x").status_code == 403
    assert client.put(
        f"/api/admin/users/{target}/llm-config/fallback",
        json={"fallback_profile_id": None},
    ).status_code == 403


def test_admin_creates_profile_visible_to_owner(client):
    target = user_id_of(client, "testuser")
    login(client, "fleet-admin", "admin-test-password")
    created = client.post(
        f"/api/admin/users/{target}/llm-config-profiles",
        json=profile_payload(),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "Fleet Route"
    assert body["upstream_api_key"] == "admin-provided-key"

    # The owner sees the admin-provisioned profile in their own session.
    client.cookies.clear()
    login(client, "testuser", "testpass")
    listing = client.get("/api/me/llm-config-profiles").json()["profiles"]
    assert len(listing) == 1
    assert listing[0]["name"] == "Fleet Route"


def test_admin_lists_user_profiles_with_keys(client):
    target = user_id_of(client, "testuser")
    login(client, "fleet-admin", "admin-test-password")
    client.post(
        f"/api/admin/users/{target}/llm-config-profiles", json=profile_payload(),
    )
    listing = client.get(f"/api/admin/users/{target}/llm-config-profiles")
    assert listing.status_code == 200
    profiles = listing.json()["profiles"]
    assert profiles[0]["upstream_api_key"] == "admin-provided-key"


def test_admin_updates_and_deletes_user_profile(client):
    target = user_id_of(client, "testuser")
    login(client, "fleet-admin", "admin-test-password")
    created = client.post(
        f"/api/admin/users/{target}/llm-config-profiles", json=profile_payload(),
    ).json()

    update = profile_payload(name="Renamed", service_name="Renamed Route")
    update.pop("upstream_api_key")
    updated = client.put(
        f"/api/admin/users/{target}/llm-config-profiles/{created['id']}",
        json=update,
    )
    assert updated.status_code == 200
    assert updated.json()["service_name"] == "Renamed Route"
    # Key preserved when omitted.
    assert updated.json()["upstream_api_key"] == "admin-provided-key"

    deleted = client.delete(
        f"/api/admin/users/{target}/llm-config-profiles/{created['id']}",
    )
    assert deleted.status_code == 200
    assert client.get(
        f"/api/admin/users/{target}/llm-config-profiles",
    ).json()["profiles"] == []


def test_admin_activates_user_profile_bumps_version(client):
    target = user_id_of(client, "testuser")
    login(client, "fleet-admin", "admin-test-password")
    before = client.get(f"/api/admin/users/{target}/llm-config-profiles")
    # Seed an active config through the admin route editor first.
    active = client.put(
        f"/api/admin/users/{target}/llm-config",
        json={k: v for k, v in profile_payload().items() if k != "name"},
    ).json()
    created = client.post(
        f"/api/admin/users/{target}/llm-config-profiles",
        json=profile_payload(name="Backup", service_name="Backup Managed"),
    ).json()

    activated = client.post(
        f"/api/admin/users/{target}/llm-config-profiles/{created['id']}/activate",
    )
    assert activated.status_code == 200
    body = activated.json()
    assert body["config_version"] == active["config_version"] + 1
    assert body["service_name"] == "Backup Managed"
    assert body["active_profile_id"] == created["id"]
    assert before.status_code == 200


def test_admin_sets_user_fallback(client):
    target = user_id_of(client, "testuser")
    login(client, "fleet-admin", "admin-test-password")
    client.put(
        f"/api/admin/users/{target}/llm-config",
        json={k: v for k, v in profile_payload().items() if k != "name"},
    )
    created = client.post(
        f"/api/admin/users/{target}/llm-config-profiles",
        json=profile_payload(name="Backup"),
    ).json()
    response = client.put(
        f"/api/admin/users/{target}/llm-config/fallback",
        json={"fallback_profile_id": created["id"]},
    )
    assert response.status_code == 200
    assert response.json()["fallback_profile_id"] == created["id"]

    cleared = client.put(
        f"/api/admin/users/{target}/llm-config/fallback",
        json={"fallback_profile_id": None},
    )
    assert cleared.status_code == 200
    assert cleared.json()["fallback_profile_id"] is None


def test_admin_profile_apis_reject_unknown_user(client):
    login(client, "fleet-admin", "admin-test-password")
    base = "/api/admin/users/missing/llm-config-profiles"
    assert client.get(base).status_code == 404
    assert client.post(base, json=profile_payload()).status_code == 404
    assert client.post(f"{base}/x/activate").status_code == 404
    assert client.put(
        "/api/admin/users/missing/llm-config/fallback",
        json={"fallback_profile_id": None},
    ).status_code == 404


def test_admin_cannot_see_or_edit_foreign_profile_ids(client):
    """Profile endpoints are scoped to the path user; cross-user ids 404."""
    target = user_id_of(client, "testuser")
    login(client, "fleet-admin", "admin-test-password")
    created = client.post(
        f"/api/admin/users/{target}/llm-config-profiles", json=profile_payload(),
    ).json()

    # A different user's session cannot touch it through the self API.
    client.cookies.clear()
    client.post("/register", json={"username": "mallory", "password": "Passw0rd-strong"})
    login(client, "mallory", "Passw0rd-strong")
    assert client.post(
        f"/api/me/llm-config-profiles/{created['id']}/activate",
    ).status_code == 404
    assert client.get(
        f"/api/me/llm-config-profiles/{created['id']}",
    ).status_code == 404
