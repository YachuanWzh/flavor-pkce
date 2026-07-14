"""Test API Gateway JWT verification and proxying."""
import os
import tempfile
import pytest
from fastapi.testclient import TestClient

import gateway.config as config


@pytest.fixture(autouse=True)
def setup_keys():
    """Ensure keys exist for testing."""
    # Use auth-server's keys
    keys_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "auth-server", "auth_server", "keys"
    )
    if not os.path.exists(keys_dir):
        os.makedirs(keys_dir, exist_ok=True)

    # Generate keys if missing by importing auth_server jwt_utils
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auth-server"))
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()

    config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
    config.UPSTREAM_URL = "https://httpbin.org"
    config.UPSTREAM_API_KEY = ""
    yield


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


def test_missing_auth_header(client):
    """Requests without Authorization header should return 401."""
    resp = client.get("/v1/models")
    assert resp.status_code == 401


def test_invalid_jwt(client):
    """Requests with invalid JWT should return 401."""
    resp = client.get("/v1/models", headers={
        "Authorization": "Bearer invalid.jwt.token"
    })
    assert resp.status_code == 401


def test_valid_jwt_passes_verification(client):
    """Requests with valid JWT should be proxied."""
    import time
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization as ser
    import gateway.main as gm

    # Generate a fresh key pair for this test
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

    # Write public key to temp file for gateway
    tmp_pub = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    tmp_pub.write(public_pem)
    tmp_pub.close()

    old_key_path = config.JWT_PUBLIC_KEY_PATH
    config.JWT_PUBLIC_KEY_PATH = tmp_pub.name
    gm._public_key = None

    # Create a valid JWT
    now = int(time.time())
    payload = {"sub": "test", "client_id": "test", "scope": "test",
               "iat": now, "exp": now + 3600, "jti": "test-jti"}
    token = pyjwt.encode(payload, private_key, algorithm="RS256")

    resp = client.get("/v1/models", headers={
        "Authorization": f"Bearer {token}"
    })
    assert resp.status_code != 401

    # Cleanup
    config.JWT_PUBLIC_KEY_PATH = old_key_path
    gm._public_key = None
    os.unlink(tmp_pub.name)


def test_expired_jwt(client):
    """Expired JWT should be rejected."""
    import time
    import jwt as pyjwt

    # Generate a public key matching what the gateway expects
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )
    public_key = private_key.public_key()
    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Point gateway to this temp public key
    tmp_key = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    tmp_key.write(public_pem)
    tmp_key.close()

    old_key_path = config.JWT_PUBLIC_KEY_PATH
    config.JWT_PUBLIC_KEY_PATH = tmp_key.name

    # Reset cached public key
    import gateway.main as gm
    gm._public_key = None

    # Create expired JWT
    now = int(time.time())
    payload = {"sub": "test", "client_id": "test", "iat": now - 7200, "exp": now - 3600, "jti": "test"}
    token = pyjwt.encode(payload, private_key, algorithm="RS256")

    resp = client.get("/v1/models", headers={
        "Authorization": f"Bearer {token}"
    })
    assert resp.status_code == 401

    # Cleanup
    config.JWT_PUBLIC_KEY_PATH = old_key_path
    gm._public_key = None
    os.unlink(tmp_key.name)
