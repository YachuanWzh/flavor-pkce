"""Gateway-side revocation checks (P0-3): a JWT whose jti was revoked at the
auth server must be rejected before proxying.
"""
import os
import time
import tempfile

import pytest
import jwt as pyjwt
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization as ser

import gateway.config as config
import gateway.main as gm


@pytest.fixture(autouse=True)
def setup_keys():
    """Ensure the shared RSA keys exist for testing."""
    keys_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-server", "auth_server", "keys"
    )
    if not os.path.exists(keys_dir):
        os.makedirs(keys_dir, exist_ok=True)
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auth-server"))
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()
    config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
    yield


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


def _sign_token(jti: str) -> str:
    """Sign a valid JWT with a fresh key and point the gateway at it."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=ser.Encoding.PEM,
        format=ser.PublicFormat.SubjectPublicKeyInfo,
    )
    private_pem = private_key.private_bytes(
        encoding=ser.Encoding.PEM,
        format=ser.PrivateFormat.PKCS8,
        encryption_algorithm=ser.NoEncryption(),
    )
    tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    tmp.write(public_pem)
    tmp.close()
    config.JWT_PUBLIC_KEY_PATH = tmp.name
    gm._public_key = None

    now = int(time.time())
    payload = {
        "sub": "user-1", "client_id": "flavor-code-cli", "scope": "models:use",
        "iat": now, "exp": now + 3600, "jti": jti,
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256"), tmp.name


def test_revoked_jti_rejected(client, monkeypatch):
    """When the auth server reports the jti revoked, the request is 401."""
    async def fake_revoked(jti: str) -> bool:
        return True
    monkeypatch.setattr(gm, "_is_jti_revoked", fake_revoked)

    token, tmp_pub = _sign_token("revoked-jti-123")
    try:
        resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 401
        assert "revoked" in resp.text.lower()
    finally:
        config.JWT_PUBLIC_KEY_PATH = os.path.join(
            os.path.dirname(__file__), "..", "..", "auth-server",
            "auth_server", "keys", "public.pem",
        )
        gm._public_key = None
        os.unlink(tmp_pub)


def test_active_jti_passes_revocation_check(client, monkeypatch):
    """An unrevolved jti is allowed through to routing/proxy."""
    async def fake_revoked(jti: str) -> bool:
        return False
    monkeypatch.setattr(gm, "_is_jti_revoked", fake_revoked)

    token, tmp_pub = _sign_token("active-jti-456")
    try:
        resp = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        # Must NOT be rejected with the revocation error
        assert resp.status_code != 401
        assert "revoked" not in resp.text.lower()
    finally:
        config.JWT_PUBLIC_KEY_PATH = os.path.join(
            os.path.dirname(__file__), "..", "..", "auth-server",
            "auth_server", "keys", "public.pem",
        )
        gm._public_key = None
        os.unlink(tmp_pub)
