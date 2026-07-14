"""JWT utilities for PKCE authorization server.

Uses RS256 (RSA with SHA-256) for JWT signing.
Keys are generated on first run and stored in auth_server/keys/.
"""
import os
import uuid
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

from auth_server.config import (
    JWT_PRIVATE_KEY_PATH,
    JWT_PUBLIC_KEY_PATH,
    JWT_ALGORITHM,
    JWT_EXPIRES_IN,
)


def _ensure_keys_exist() -> None:
    """Generate RSA key pair if they don't exist."""
    keys_dir = Path(JWT_PRIVATE_KEY_PATH).parent
    keys_dir.mkdir(parents=True, exist_ok=True)

    if os.path.exists(JWT_PRIVATE_KEY_PATH) and os.path.exists(JWT_PUBLIC_KEY_PATH):
        return

    # Generate RSA key pair
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )

    # Write private key
    with open(JWT_PRIVATE_KEY_PATH, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    # Write public key
    public_key = private_key.public_key()
    with open(JWT_PUBLIC_KEY_PATH, "wb") as f:
        f.write(public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))


def _load_private_key():
    """Load the RSA private key."""
    _ensure_keys_exist()
    with open(JWT_PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(
            f.read(),
            password=None,
            backend=default_backend(),
        )


def create_jwt(sub: str, client_id: str, scope: str = "") -> str:
    """Create a signed JWT access token.

    Args:
        sub: User ID (subject claim)
        client_id: OAuth client ID
        scope: Space-separated scopes

    Returns:
        Signed JWT string
    """
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())

    payload = {
        "sub": sub,
        "client_id": client_id,
        "scope": scope,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=JWT_EXPIRES_IN)).timestamp()),
        "jti": jti,
    }

    private_key = _load_private_key()
    token = pyjwt.encode(payload, private_key, algorithm=JWT_ALGORITHM)
    return token


def verify_jwt(token: str) -> dict | None:
    """Verify a JWT and return its payload. Returns None if invalid."""
    try:
        _ensure_keys_exist()
        with open(JWT_PUBLIC_KEY_PATH, "rb") as f:
            public_key = serialization.load_pem_public_key(
                f.read(),
                backend=default_backend(),
            )
        return pyjwt.decode(token, public_key, algorithms=[JWT_ALGORITHM])
    except Exception:
        return None


def get_jwt_payload(token: str) -> dict | None:
    """Decode JWT payload without verification (for extracting claims)."""
    try:
        return pyjwt.decode(token, options={"verify_signature": False})
    except Exception:
        return None
