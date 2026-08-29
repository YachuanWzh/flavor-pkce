"""Gateway resolves signing keys by kid; unknown kid triggers a JWKS
refresh (improvement 5, verification side)."""

import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import gateway.config as config
import gateway.main as gm


def _pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key


def _token(key, kid=None):
    now = int(time.time())
    payload = {"sub": "u", "exp": now + 300, "iat": now, "role": "admin"}
    headers = {"kid": kid} if kid else None
    return pyjwt.encode(payload, key, algorithm="RS256", headers=headers)


@pytest.fixture
def keyring_env(tmp_path):
    old = (config.JWT_PUBLIC_KEY_PATH, config.AUTH_SERVER_INTERNAL_URL,
           getattr(config, "JWT_JWKS_URL", None),
           getattr(config, "JWT_JWKS_REFETCH_SECONDS", None))
    key1 = _pair()
    pem = key1.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    p = tmp_path / "public.pem"
    p.write_bytes(pem)
    config.JWT_PUBLIC_KEY_PATH = str(p)
    gm._reset_jwt_keyring_for_tests()
    yield key1
    (config.JWT_PUBLIC_KEY_PATH, config.AUTH_SERVER_INTERNAL_URL,
     config.JWT_JWKS_URL, config.JWT_JWKS_REFETCH_SECONDS) = old
    gm._reset_jwt_keyring_for_tests()


def test_kid_token_verifies_via_bootstrap_file(keyring_env):
    key = keyring_env
    kid = gm._key_id_for_public(key.public_key())
    assert gm.verify_jwt(_token(key, kid))["sub"] == "u"


def test_unknown_kid_refreshes_jwks_once(keyring_env, monkeypatch):
    key2 = _pair()
    kid2 = gm._key_id_for_public(key2.public_key())
    calls = []

    def fake_fetch():
        calls.append(1)
        nums = key2.public_key().public_numbers()
        gm._install_jwk(kid2, nums.n, nums.e)

    monkeypatch.setattr(gm, "_fetch_jwks_into_keyring", fake_fetch)
    assert gm.verify_jwt(_token(key2, kid2))["sub"] == "u"
    assert calls == [1]
    gm.verify_jwt(_token(key2, kid2))  # cached now → no second fetch
    assert calls == [1]


def test_legacy_no_kid_token_still_verifies(keyring_env):
    assert gm.verify_jwt(_token(keyring_env))["sub"] == "u"


def test_unknown_kid_no_jwks_returns_none(keyring_env, monkeypatch):
    key3 = _pair()
    kid3 = gm._key_id_for_public(key3.public_key())
    monkeypatch.setattr(gm, "_fetch_jwks_into_keyring", lambda: None)
    assert gm.verify_jwt(_token(key3, kid3)) is None


def test_wrong_signature_for_known_kid_returns_none(keyring_env, monkeypatch):
    """A token claiming the bootstrap kid but signed elsewhere must not
    trigger endless JWKS refetching nor verify."""
    evil = _pair()
    kid1 = gm._key_id_for_public(keyring_env.public_key())
    monkeypatch.setattr(gm, "_fetch_jwks_into_keyring", lambda: None)
    assert gm.verify_jwt(_token(evil, kid1)) is None


def test_rotation_end_to_end(keyring_env, monkeypatch):
    """The full rotation story: auth restarts with a fresh key, the gateway
    picks it up from JWKS without restarting."""
    key_new = _pair()
    kid_new = gm._key_id_for_public(key_new.public_key())

    def fake_fetch():
        nums = key_new.public_key().public_numbers()
        gm._install_jwk(kid_new, nums.n, nums.e)

    monkeypatch.setattr(gm, "_fetch_jwks_into_keyring", fake_fetch)
    assert gm.verify_jwt(_token(key_new, kid_new))["sub"] == "u"
    # The pre-rotation key still verifies until its tokens expire.
    kid_old = gm._key_id_for_public(keyring_env.public_key())
    assert gm.verify_jwt(_token(keyring_env, kid_old))["sub"] == "u"
