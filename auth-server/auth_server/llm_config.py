"""Encrypted persistence helpers for per-user LLM configuration."""
import json
import os
import uuid
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
        "active_profile_id": row["active_profile_id"]
        if "active_profile_id" in row.keys() else None,
        "fallback_profile_id": row["fallback_profile_id"]
        if "fallback_profile_id" in row.keys() else None,
        "updated_at": row["updated_at"],
    }
    if include_secret:
        result["upstream_api_key"] = decrypt_api_key(
            row["upstream_api_key_encrypted"],
        )
    return result


def save_llm_config(
    user_id: str, values: dict, *, active_profile_id: str | None = None,
    include_secret: bool = False,
) -> dict:
    current = get_llm_config(user_id, include_secret=True)
    if values.get("clear_api_key"):
        api_key = ""
    elif values.get("upstream_api_key") is not None:
        api_key = values["upstream_api_key"]
    else:
        api_key = current.get("upstream_api_key", "") if current else ""
    version = (current["config_version"] if current else 0) + 1
    # A direct (non-profile) save detaches the active-profile link; profile
    # activation passes the source profile id explicitly. The fallback link
    # always survives — it is an independent routing decision.
    fallback_profile_id = (
        current.get("fallback_profile_id") if current else None
    )
    encrypted = encrypt_api_key(api_key)
    db = get_db()
    db.execute(
        """INSERT INTO user_llm_configs (
               user_id, provider_id, service_name, api_type, upstream_url,
               upstream_api_key_encrypted, upstream_auth_type, default_model,
               cheap_model, models, max_output_tokens, config_version,
               active_profile_id, fallback_profile_id, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
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
               active_profile_id=excluded.active_profile_id,
               fallback_profile_id=excluded.fallback_profile_id,
               updated_at=datetime('now')""",
        (
            user_id, values["provider_id"], values["service_name"],
            values["api_type"], values["upstream_url"], encrypted,
            values["upstream_auth_type"], values["default_model"],
            values["cheap_model"], json.dumps(values["models"]),
            values["max_output_tokens"], version,
            active_profile_id, fallback_profile_id,
        ),
    )
    db.commit()
    db.close()
    return get_llm_config(user_id, include_secret=include_secret) or {}


# ---------------------------------------------------------------------------
# Named configuration profiles
# ---------------------------------------------------------------------------

def _profile_row_to_dict(row, *, include_secret: bool = False) -> dict:
    result = {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["name"],
        "provider_id": row["provider_id"],
        "service_name": row["service_name"],
        "api_type": row["api_type"],
        "upstream_url": row["upstream_url"],
        "upstream_auth_type": row["upstream_auth_type"],
        "default_model": row["default_model"],
        "cheap_model": row["cheap_model"],
        "models": json.loads(row["models"]),
        "max_output_tokens": row["max_output_tokens"],
        "api_key_configured": bool(row["upstream_api_key_encrypted"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_secret:
        result["upstream_api_key"] = decrypt_api_key(
            row["upstream_api_key_encrypted"],
        )
    return result


def list_profiles(user_id: str, *, include_secret: bool = False) -> list[dict]:
    db = get_db()
    rows = db.execute(
        "SELECT * FROM llm_config_profiles WHERE user_id = ? "
        "ORDER BY created_at, name",
        (user_id,),
    ).fetchall()
    db.close()
    return [
        _profile_row_to_dict(row, include_secret=include_secret) for row in rows
    ]


def get_profile(user_id: str, profile_id: str, *, include_secret: bool = False) -> dict | None:
    db = get_db()
    row = db.execute(
        "SELECT * FROM llm_config_profiles WHERE id = ? AND user_id = ?",
        (profile_id, user_id),
    ).fetchone()
    db.close()
    if row is None:
        return None
    return _profile_row_to_dict(row, include_secret=include_secret)


def profile_name_exists(user_id: str, name: str, *, exclude_id: str | None = None) -> bool:
    db = get_db()
    row = db.execute(
        "SELECT id FROM llm_config_profiles WHERE user_id = ? AND name = ?",
        (user_id, name),
    ).fetchone()
    db.close()
    return row is not None and row["id"] != exclude_id


def create_profile(user_id: str, values: dict, *, include_secret: bool = False) -> dict:
    profile_id = str(uuid.uuid4())
    encrypted = encrypt_api_key(values.get("upstream_api_key") or "")
    db = get_db()
    db.execute(
        """INSERT INTO llm_config_profiles (
               id, user_id, name, provider_id, service_name, api_type,
               upstream_url, upstream_api_key_encrypted, upstream_auth_type,
               default_model, cheap_model, models, max_output_tokens,
               created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
        (
            profile_id, user_id, values["name"], values["provider_id"],
            values["service_name"], values["api_type"], values["upstream_url"],
            encrypted, values["upstream_auth_type"], values["default_model"],
            values["cheap_model"], json.dumps(values["models"]),
            values["max_output_tokens"],
        ),
    )
    db.commit()
    db.close()
    return get_profile(user_id, profile_id, include_secret=include_secret) or {}


def update_profile(
    user_id: str, profile_id: str, values: dict, *, include_secret: bool = False,
) -> dict | None:
    current = get_profile(user_id, profile_id, include_secret=True)
    if current is None:
        return None
    if values.get("upstream_api_key") is not None:
        api_key = values["upstream_api_key"]
    else:
        api_key = current.get("upstream_api_key", "")
    encrypted = encrypt_api_key(api_key)
    db = get_db()
    db.execute(
        """UPDATE llm_config_profiles SET
               name=?, provider_id=?, service_name=?, api_type=?,
               upstream_url=?, upstream_api_key_encrypted=?,
               upstream_auth_type=?, default_model=?, cheap_model=?,
               models=?, max_output_tokens=?, updated_at=datetime('now')
           WHERE id = ? AND user_id = ?""",
        (
            values["name"], values["provider_id"], values["service_name"],
            values["api_type"], values["upstream_url"], encrypted,
            values["upstream_auth_type"], values["default_model"],
            values["cheap_model"], json.dumps(values["models"]),
            values["max_output_tokens"], profile_id, user_id,
        ),
    )
    db.commit()
    db.close()
    return get_profile(user_id, profile_id, include_secret=include_secret)


def delete_profile(user_id: str, profile_id: str) -> bool:
    db = get_db()
    cursor = db.execute(
        "DELETE FROM llm_config_profiles WHERE id = ? AND user_id = ?",
        (profile_id, user_id),
    )
    deleted = cursor.rowcount > 0
    if deleted:
        # Clear dangling links from the active configuration.
        db.execute(
            "UPDATE user_llm_configs SET active_profile_id = NULL "
            "WHERE user_id = ? AND active_profile_id = ?",
            (user_id, profile_id),
        )
        db.execute(
            "UPDATE user_llm_configs SET fallback_profile_id = NULL "
            "WHERE user_id = ? AND fallback_profile_id = ?",
            (user_id, profile_id),
        )
    db.commit()
    db.close()
    return deleted

