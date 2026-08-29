"""JWT kid header + /.well-known/jwks.json (improvement 5, signing side).

The kid is the SHA-256 thumbprint (first 16 hex chars) of the public key's
DER encoding. The JWKS endpoint exposes the current signing key so the
gateway can resolve tokens by kid after a key rotation.
"""

import base64
import os
import tempfile

import jwt as pyjwt
import pytest

import auth_server.config as config
from auth_server import jwt_utils
from auth_server.database import init_db


def _b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture(autouse=True)
def clean_keys(tmp_path, monkeypatch):
    """Isolated key pair per test (never touches the real keys/ dir)."""
    monkeypatch.setattr(jwt_utils, "JWT_PRIVATE_KEY_PATH",
                        str(tmp_path / "private.pem"))
    monkeypatch.setattr(jwt_utils, "JWT_PUBLIC_KEY_PATH",
                        str(tmp_path / "public.pem"))
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(config, "DB_PATH", db)
    init_db()
    yield
    if os.path.exists(db):
        os.remove(db)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from auth_server.main import app
    return TestClient(app)


def test_create_jwt_has_kid_header():
    token = jwt_utils.create_jwt(sub="u1", client_id="c", username="u1")
    header = pyjwt.get_unverified_header(token)
    assert len(header.get("kid", "")) == 16


def test_kid_is_stable_per_key_and_changes_on_rotation():
    kid_a = pyjwt.get_unverified_header(
        jwt_utils.create_jwt(sub="u", client_id="c")
    )["kid"]
    kid_b = pyjwt.get_unverified_header(
        jwt_utils.create_jwt(sub="u", client_id="c")
    )["kid"]
    assert kid_a == kid_b
    # Rotate: wipe the key pair — the next issue regenerates a fresh one.
    os.remove(jwt_utils.JWT_PRIVATE_KEY_PATH)
    os.remove(jwt_utils.JWT_PUBLIC_KEY_PATH)
    kid_c = pyjwt.get_unverified_header(
        jwt_utils.create_jwt(sub="u", client_id="c")
    )["kid"]
    assert kid_c != kid_a


def test_jwks_endpoint_publishes_current_kid(client):
    token = jwt_utils.create_jwt(sub="u1", client_id="c", username="u1")
    kid = pyjwt.get_unverified_header(token)["kid"]
    resp = client.get("/.well-known/jwks.json")
    assert resp.status_code == 200
    jwks = resp.json()
    key = next(k for k in jwks["keys"] if k["kid"] == kid)
    assert key["kty"] == "RSA" and key["alg"] == "RS256" and key["use"] == "sig"
    assert key.get("n") and key.get("e")


def test_jwks_public_key_verifies_issued_token(client):
    token = jwt_utils.create_jwt(sub="u1", client_id="c", username="u1")
    jwk = client.get("/.well-known/jwks.json").json()["keys"][0]
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    n = int.from_bytes(_b64u(jwk["n"]), "big")
    e = int.from_bytes(_b64u(jwk["e"]), "big")
    pub = RSAPublicNumbers(e, n).public_key()
    payload = pyjwt.decode(token, pub, algorithms=["RS256"])
    assert payload["sub"] == "u1"


def test_jwks_endpoint_is_public_no_cookie(client):
    """JWKS carries only public material — no auth required."""
    assert client.get("/.well-known/jwks.json").status_code == 200
