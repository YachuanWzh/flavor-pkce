"""Encrypted persistence helpers for per-user LLM configuration."""
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet

import auth_server.config as config
from auth_server.database import get_db


def _fernet() -> Fernet:
    path = Path(config.LLM_CONFIG_ENCRYPTION_KEY_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return Fernet(path.read_bytes().strip())


def encrypt_api_key(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_api_key(value: str) -> str:
    if not value:
        return ""
    return _fernet().decrypt(value.encode("ascii")).decode("utf-8")


def get_llm_config(user_id: str, *, include_secret: bool = False) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT * FROM user_llm_configs WHERE user_id = ?", (user_id,),
    ).fetchone()
    db.close()
    if row is None:
        return None
    result = {
        "user_id": row["user_id"],
        "provider_id": row["provider_id"],
        "service_name": row["service_name"],
        "api_type": row["api_type"],
        "upstream_url": row["upstream_url"],
        "upstream_auth_type": row["upstream_auth_type"],
        "default_model": row["default_model"],
        "cheap_model": row["cheap_model"],
        "models": json.loads(row["models"]),
        "max_output_tokens": row["max_output_tokens"],
        "config_version": row["config_version"],
        "api_key_configured": bool(row["upstream_api_key_encrypted"]),
        "updated_at": row["updated_at"],
    }
    if include_secret:
        result["upstream_api_key"] = decrypt_api_key(
            row["upstream_api_key_encrypted"],
        )
    return result


def save_llm_config(user_id: str, values: dict) -> dict:
    current = get_llm_config(user_id, include_secret=True)
    if values.get("clear_api_key"):
        api_key = ""
    elif values.get("upstream_api_key") is not None:
        api_key = values["upstream_api_key"]
    else:
        api_key = current.get("upstream_api_key", "") if current else ""
    version = (current["config_version"] if current else 0) + 1
    encrypted = encrypt_api_key(api_key)
    db = get_db()
    db.execute(
        """INSERT INTO user_llm_configs (
               user_id, provider_id, service_name, api_type, upstream_url,
               upstream_api_key_encrypted, upstream_auth_type, default_model,
               cheap_model, models, max_output_tokens, config_version, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(user_id) DO UPDATE SET
               provider_id=excluded.provider_id,
               service_name=excluded.service_name,
               api_type=excluded.api_type,
               upstream_url=excluded.upstream_url,
               upstream_api_key_encrypted=excluded.upstream_api_key_encrypted,
               upstream_auth_type=excluded.upstream_auth_type,
               default_model=excluded.default_model,
               cheap_model=excluded.cheap_model,
               models=excluded.models,
               max_output_tokens=excluded.max_output_tokens,
               config_version=excluded.config_version,
               updated_at=datetime('now')""",
        (
            user_id, values["provider_id"], values["service_name"],
            values["api_type"], values["upstream_url"], encrypted,
            values["upstream_auth_type"], values["default_model"],
            values["cheap_model"], json.dumps(values["models"]),
            values["max_output_tokens"], version,
        ),
    )
    db.commit()
    db.close()
    return get_llm_config(user_id) or {}

