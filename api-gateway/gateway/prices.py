"""Admin-managed per-model USD prices (per 1M tokens).

Historically prices lived only in the ``MODEL_PRICES_JSON`` env var, so
changing them required a redeploy and unpriced models showed $0 on the
dashboard. This module adds a ``model_prices`` table editable through the
admin API; the effective price table used by cost reports and quota
accounting is the env config merged with the DB (DB wins on conflicts).

The :data:`PRICE_CATALOG` is a small set of public list prices offered as
one-click defaults in the admin UI. Prices drift with provider pricing
pages, so treat the catalog as a starting point, not a source of truth.
"""

from datetime import datetime, timezone

import gateway.config
from gateway.database import _connect

PRICE_FIELDS = ("prompt", "completion", "cache_read", "cache_creation")

# USD per 1M tokens (public list prices; editable once added).
PRICE_CATALOG: list[dict] = [
    {"model": "gpt-4o", "prompt": 2.5, "completion": 10.0, "cache_read": 1.25, "cache_creation": 2.5},
    {"model": "gpt-4o-mini", "prompt": 0.15, "completion": 0.6, "cache_read": 0.075, "cache_creation": 0.15},
    {"model": "gpt-4.1", "prompt": 2.0, "completion": 8.0, "cache_read": 1.0, "cache_creation": 2.0},
    {"model": "o3-mini", "prompt": 1.1, "completion": 4.4, "cache_read": 0.55, "cache_creation": 1.1},
    {"model": "claude-sonnet-4-5", "prompt": 3.0, "completion": 15.0, "cache_read": 0.3, "cache_creation": 3.75},
    {"model": "claude-opus-4-1", "prompt": 15.0, "completion": 75.0, "cache_read": 1.5, "cache_creation": 18.75},
    {"model": "deepseek-chat", "prompt": 0.27, "completion": 1.1, "cache_read": 0.07, "cache_creation": 0.27},
    {"model": "deepseek-reasoner", "prompt": 0.55, "completion": 2.19, "cache_read": 0.14, "cache_creation": 0.55},
    {"model": "qwen-plus", "prompt": 0.8, "completion": 2.0, "cache_read": 0.16, "cache_creation": 0.8},
    {"model": "qwen-turbo", "prompt": 0.05, "completion": 0.2, "cache_read": 0.01, "cache_creation": 0.05},
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_model_prices() -> list[dict]:
    """All admin-configured prices, newest model name first by sort key."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM model_prices ORDER BY model"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def upsert_model_price(
    model: str,
    *,
    prompt: float = 0.0,
    completion: float = 0.0,
    cache_read: float = 0.0,
    cache_creation: float = 0.0,
) -> dict:
    """Insert or update one model's price row."""
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    given = {
        "prompt": prompt, "completion": completion,
        "cache_read": cache_read, "cache_creation": cache_creation,
    }
    values = []
    for field in PRICE_FIELDS:
        value = float(given[field])
        if value < 0:
            raise ValueError(f"{field} must not be negative")
        values.append(value)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO model_prices (model, prompt, completion, cache_read, cache_creation, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(model) DO UPDATE SET
                   prompt = excluded.prompt,
                   completion = excluded.completion,
                   cache_read = excluded.cache_read,
                   cache_creation = excluded.cache_creation,
                   updated_at = excluded.updated_at""",
            (model, *values, _now()),
        )
        row = conn.execute(
            "SELECT * FROM model_prices WHERE model = ?", (model,)
        ).fetchone()
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def delete_model_price(model: str) -> bool:
    """Delete one price row. Returns True when a row was removed."""
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM model_prices WHERE model = ?",
            ((model or "").strip(),),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def catalog_entries() -> list[dict]:
    """Public default prices plus a ``configured`` flag per entry."""
    conn = _connect()
    try:
        existing = {
            row["model"]
            for row in conn.execute("SELECT model FROM model_prices")
        }
    finally:
        conn.close()
    return [
        {**entry, "configured": entry["model"] in existing}
        for entry in PRICE_CATALOG
    ]


def effective_prices() -> dict:
    """Price table used by cost reports and quota accounting.

    Env-configured prices are the base; DB rows override per model. Read
    per call so admin edits take effect without a restart (and so tests
    can keep monkeypatching ``config.MODEL_PRICES``).
    """
    merged: dict = dict(gateway.config.MODEL_PRICES)
    for row in list_model_prices():
        model = row.pop("model")
        row.pop("updated_at", None)
        merged[model] = row
    return merged
