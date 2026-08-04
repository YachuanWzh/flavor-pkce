"""Rate limiting and brute-force lockout helpers (P0-4).

All state is persisted in SQLite so limits survive restarts.  Keys are
namespaced by the caller, e.g. ``login:ip:1.2.3.4`` or ``login:user:alice``.
"""

import time

from auth_server.database import get_db


def _bucket(now: float, window_seconds: int) -> int:
    """Fixed-window bucket index for ``now``."""
    return int(now // window_seconds)


def record_attempt(key: str, *, window_seconds: int, now: float | None = None) -> None:
    """Count one attempt toward the sliding window for ``key``."""
    now = time.time() if now is None else now
    bucket = _bucket(now, window_seconds)
    db = get_db()
    db.execute(
        """INSERT INTO rate_limits (key, window_start, hits) VALUES (?, ?, 1)
           ON CONFLICT(key) DO UPDATE SET
               window_start = excluded.window_start,
               hits = CASE
                   WHEN rate_limits.window_start = excluded.window_start
                       THEN rate_limits.hits + 1
                   ELSE 1 END""",
        (key, bucket),
    )
    db.commit()
    db.close()


def is_rate_limited(
    key: str, *, limit: int, window_seconds: int, now: float | None = None,
) -> bool:
    """True when ``key`` exceeded ``limit`` hits in this window.

    The caller records the current attempt *before* checking, so up to
    ``limit`` requests per window are allowed and the next one is rejected.
    """
    now = time.time() if now is None else now
    bucket = _bucket(now, window_seconds)
    db = get_db()
    row = db.execute(
        "SELECT hits FROM rate_limits WHERE key = ? AND window_start = ?",
        (key, bucket),
    ).fetchone()
    db.close()
    return row is not None and row["hits"] > limit


def record_failure(
    key: str, *, max_failures: int, lock_seconds: int,
    now: float | None = None,
) -> bool:
    """Record a failed attempt for ``key``; returns True when it just locked."""
    now = time.time() if now is None else now
    db = get_db()
    row = db.execute(
        "SELECT failures, locked_until FROM login_failures WHERE key = ?", (key,),
    ).fetchone()
    failures = (row["failures"] if row else 0) + 1
    locked_until = now + lock_seconds if failures >= max_failures else (
        row["locked_until"] if row else 0.0
    )
    db.execute(
        """INSERT INTO login_failures (key, failures, locked_until) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET
               failures = excluded.failures,
               locked_until = excluded.locked_until""",
        (key, failures, locked_until),
    )
    db.commit()
    db.close()
    return failures >= max_failures


def is_locked(key: str, *, now: float | None = None) -> bool:
    """True while ``key`` is inside its lockout window."""
    now = time.time() if now is None else now
    db = get_db()
    row = db.execute(
        "SELECT locked_until FROM login_failures WHERE key = ?", (key,),
    ).fetchone()
    db.close()
    return row is not None and row["locked_until"] > now


def reset_failures(key: str) -> None:
    """Clear recorded failures for ``key`` (after a successful login)."""
    db = get_db()
    db.execute("DELETE FROM login_failures WHERE key = ?", (key,))
    db.commit()
    db.close()
