"""Super-admin management of user roles (admin / user)."""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

import auth_server.config as config
from auth_server.database import init_db, get_db


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db", prefix="role_mgmt_test_")
    os.close(fd)
    previous = config.DB_PATH
    config.DB_PATH = path
    config.SEED_TEST_USER = True
    monkeypatch.setattr(config, "ADMIN_USERNAME", "super-admin", raising=False)
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


def register(client, username, password="Passw0rd-strong"):
    response = client.post(
        "/register", json={"username": username, "password": password},
    )
    assert response.status_code == 201


def login(client, username, password):
    response = client.post("/login", data={"username": username, "password": password})
    assert response.status_code == 200


def user_id_of(client, username):
    db = get_db()
    row = db.execute(
        "SELECT id FROM users WHERE username = ?", (username,),
    ).fetchone()
    db.close()
    return row["id"]


def test_admin_promotes_user_to_admin(client):
    register(client, "alice")
    target = user_id_of(client, "alice")

    login(client, "super-admin", "admin-test-password")
    response = client.put(
        f"/api/admin/users/{target}/role", json={"role": "admin"},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "admin"

    # The new admin can now use admin APIs.
    client.cookies.clear()
    login(client, "alice", "Passw0rd-strong")
    assert client.get("/api/me").json()["role"] == "admin"
    assert client.get("/api/admin/users").status_code == 200


def test_admin_demotes_admin_to_user(client):
    register(client, "bob")
    target = user_id_of(client, "bob")
    login(client, "super-admin", "admin-test-password")
    assert client.put(f"/api/admin/users/{target}/role", json={"role": "admin"}).status_code == 200
    response = client.put(f"/api/admin/users/{target}/role", json={"role": "user"})
    assert response.status_code == 200
    assert response.json()["role"] == "user"

    client.cookies.clear()
    login(client, "bob", "Passw0rd-strong")
    assert client.get("/api/me").json()["role"] == "user"
    assert client.get("/api/admin/users").status_code == 403


def test_admin_cannot_change_own_role(client):
    login(client, "super-admin", "admin-test-password")
    me = client.get("/api/me").json()
    response = client.put(f"/api/admin/users/{me['id']}/role", json={"role": "user"})
    assert response.status_code == 400
    assert response.json()["detail"] == "self_role_change_forbidden"
    # Still admin.
    assert client.get("/api/me").json()["role"] == "admin"


def test_last_admin_cannot_be_demoted(client):
    # Seed a single-admin deployment: only "super-admin" has role=admin.
    register(client, "carol")
    carol = user_id_of(client, "carol")
    login(client, "super-admin", "admin-test-password")
    # Promote carol, then have carol (admin) demote super-admin, leaving
    # carol as the sole admin.
    client.put(f"/api/admin/users/{carol}/role", json={"role": "admin"})
    client.cookies.clear()
    login(client, "carol", "Passw0rd-strong")
    super_id = user_id_of(client, "super-admin")
    assert client.put(
        f"/api/admin/users/{super_id}/role", json={"role": "user"},
    ).status_code == 200

    # carol is now the only admin: demoting carol must be rejected.
    response = client.put(
        f"/api/admin/users/{carol}/role", json={"role": "user"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "self_role_change_forbidden"


def test_demotion_allowed_while_another_admin_remains(client):
    register(client, "dave")
    dave = user_id_of(client, "dave")
    login(client, "super-admin", "admin-test-password")
    client.put(f"/api/admin/users/{dave}/role", json={"role": "admin"})

    # super-admin demotes dave: allowed because super-admin remains admin.
    response = client.put(f"/api/admin/users/{dave}/role", json={"role": "user"})
    assert response.status_code == 200
    assert response.json()["role"] == "user"


def test_regular_user_cannot_change_roles(client):
    register(client, "eve")
    target = user_id_of(client, "eve")
    login(client, "eve", "Passw0rd-strong")
    response = client.put(f"/api/admin/users/{target}/role", json={"role": "admin"})
    assert response.status_code == 403


def test_invalid_role_rejected(client):
    register(client, "frank")
    target = user_id_of(client, "frank")
    login(client, "super-admin", "admin-test-password")
    response = client.put(f"/api/admin/users/{target}/role", json={"role": "superuser"})
    assert response.status_code == 400


def test_unknown_user_role_change_404(client):
    login(client, "super-admin", "admin-test-password")
    response = client.put("/api/admin/users/missing/role", json={"role": "admin"})
    assert response.status_code == 404
