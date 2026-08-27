"""Per-user quota and budget enforcement for the gateway proxy.

Three independent limits, each disabled when configured to 0:

- ``RATE_LIMIT_RPM``          — requests per user per fixed one-minute window.
- ``DAILY_TOKEN_BUDGET``      — prompt+completion tokens per UTC day.
- ``DAILY_COST_BUDGET_USD``   — estimated USD spend per UTC day, priced with
  ``MODEL_PRICES`` (same table the /api/stats/cost report uses).

Daily usage is persisted in the audit SQLite (``quota_usage``) so budgets
survive restarts.  The one-minute request counter deliberately shares the
same row (``minute_bucket``/``minute_hits``): a stale bucket simply resets
on first use, and cross-process contention is resolved by
``BEGIN IMMEDIATE``.  Call :func:`check` *before* proxying a request and
:func:`record_usage` *after* token usage is known.
"""

import sqlite3
import time
from datetime import datetime, timezone

import gateway.config
from gateway.database import _connect


def _utc_day(now: float | None = None) -> str:
    dt = datetime.fromtimestamp(now, timezone.utc) if now else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _minute_bucket(now: float) -> int:
    return int(now // 60)


def _cost_of(model: str | None, prompt_tokens: int, completion_tokens: int) -> float:
    prices = gateway.config.MODEL_PRICES.get(model) if model else None
    if not prices:
        return 0.0
    return (
        prompt_tokens * prices.get("prompt", 0.0)
        + completion_tokens * prices.get("completion", 0.0)
    ) / 1_000_000


def get_day_usage(user: str, day: str | None = None) -> dict:
    """Return {prompt_tokens, completion_tokens, cost} for user/day."""
    day = day or _utc_day()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT prompt_tokens, completion_tokens, cost FROM quota_usage "
            "WHERE user = ? AND day = ?",
            (user, day),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    return {
        "prompt_tokens": row["prompt_tokens"] or 0,
        "completion_tokens": row["completion_tokens"] or 0,
        "cost": row["cost"] or 0.0,
    }


def record_usage(
    user: str,
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    model: str | None = None,
    day: str | None = None,
) -> None:
    """Accumulate one request's token usage (and priced cost) into today's row.

    Never raises: quota accounting must not break the response path.
    """
    prompt_tokens = prompt_tokens or 0
    completion_tokens = completion_tokens or 0
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return
    day = day or _utc_day()
    cost = _cost_of(model, prompt_tokens, completion_tokens)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO quota_usage (user, day, prompt_tokens, completion_tokens, cost)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user, day) DO UPDATE SET
                   prompt_tokens = quota_usage.prompt_tokens + excluded.prompt_tokens,
                   completion_tokens = quota_usage.completion_tokens + excluded.completion_tokens,
                   cost = quota_usage.cost + excluded.cost""",
            (user, day, prompt_tokens, completion_tokens, cost),
        )
        conn.commit()
    except Exception:
        pass  # quota accounting is best-effort, never fatal
    finally:
        conn.close()


def check(user: str, now: float | None = None) -> tuple[bool, dict | None]:
    """Admission check for one request; records the request on success.

    Returns ``(True, None)`` when the request may proceed (and counts it
    toward the per-minute window), or ``(False, error)`` where ``error`` is
    a JSON-serialisable body for a 429 response.
    """
    now = now if now is not None else time.time()
    day = _utc_day(now)
    bucket = _minute_bucket(now)
    rpm = gateway.config.RATE_LIMIT_RPM
    token_budget = gateway.config.DAILY_TOKEN_BUDGET
    cost_budget = gateway.config.DAILY_COST_BUDGET_USD
    if rpm <= 0 and token_budget <= 0 and cost_budget <= 0:
        return True, None  # nothing configured — zero overhead

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT prompt_tokens, completion_tokens, cost, minute_bucket, minute_hits "
            "FROM quota_usage WHERE user = ? AND day = ?",
            (user, day),
        ).fetchone()
        usage = {
            "prompt_tokens": (row["prompt_tokens"] if row else 0) or 0,
            "completion_tokens": (row["completion_tokens"] if row else 0) or 0,
            "cost": (row["cost"] if row else 0.0) or 0.0,
            "minute_bucket": (row["minute_bucket"] if row else 0) or 0,
            "minute_hits": (row["minute_hits"] if row else 0) or 0,
        }

        if rpm > 0:
            hits = usage["minute_hits"] + 1 if usage["minute_bucket"] == bucket else 1
            if hits > rpm:
                conn.rollback()
                retry_after = 60 - int(now % 60)
                return False, {
                    "error": "rate_limited",
                    "message": f"Rate limit exceeded ({rpm} requests/minute)",
                    "retry_after_seconds": max(1, retry_after),
                }
        else:
            hits = usage["minute_hits"]

        if token_budget > 0:
            used = usage["prompt_tokens"] + usage["completion_tokens"]
            if used >= token_budget:
                conn.rollback()
                return False, {
                    "error": "token_budget_exceeded",
                    "message": "Daily token budget exhausted",
                    "used": used,
                    "budget": token_budget,
                }

        if cost_budget > 0 and usage["cost"] >= cost_budget:
            conn.rollback()
            return False, {
                "error": "cost_budget_exceeded",
                "message": "Daily cost budget exhausted",
                "used": round(usage["cost"], 6),
                "budget": round(cost_budget, 6),
            }

        # Admitted — count the request in the current minute window.
        conn.execute(
            """INSERT INTO quota_usage (user, day, prompt_tokens, completion_tokens, cost,
                                        minute_bucket, minute_hits)
               VALUES (?, ?, 0, 0, 0.0, ?, ?)
               ON CONFLICT(user, day) DO UPDATE SET
                   minute_bucket = excluded.minute_bucket,
                   minute_hits = excluded.minute_hits""",
            (user, day, bucket, hits),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        return True, None  # fail open: enforcement must not cause an outage
    finally:
        conn.close()
    return True, None
