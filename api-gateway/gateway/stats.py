"""Aggregated report queries over the gateway audit log.

All functions return plain dict lists so the /api/stats endpoints can
serialise them directly.  Date bucketing uses the first 10 characters of
the ISO-8601 ``timestamp`` column (the gateway always writes UTC ISO
timestamps, so ``substr(timestamp, 1, 10)`` is the UTC day).

Cache-hit semantics: ``hit_ratio`` is computed as
``cache_read / (prompt + cache_read + cache_creation)``.  For Anthropic,
``prompt_tokens`` stores non-cached input tokens; for OpenAI providers
whose ``prompt_tokens`` already include cached tokens the ratio is
approximate.  Days with no cache data report ``0.0``.
"""

import gateway.config
from gateway.database import _connect


def _where(start_date: str | None, end_date: str | None, user: str | None):
    """Build a shared WHERE clause for the report endpoints."""
    conditions: list[str] = []
    bindings: list[object] = []
    if start_date:
        conditions.append("timestamp >= ?")
        bindings.append(start_date + "T00:00:00+00:00")
    if end_date:
        conditions.append("timestamp <= ?")
        bindings.append(end_date + "T23:59:59.999999+00:00")
    if user:
        conditions.append('"user" = ?')
        bindings.append(user)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return where, bindings


def _query(sql: str, bindings: list[object]) -> list[dict]:
    conn = _connect()
    rows = conn.execute(sql, bindings).fetchall()
    conn.close()
    return [dict(row) for row in rows]


_GROUP_COLUMNS = {
    "user": '"user"',
    "model": "model",
    "service": "service_name",
}


def token_usage(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
    group_by: str | None = None,
) -> list[dict]:
    """Token usage aggregation.

    Without ``group_by`` the result is a daily time series.  With
    ``group_by`` (``user`` / ``model`` / ``service``) the result is a
    leaderboard aggregated over the whole date range — combine with the
    date filters to slice it.
    """
    where, bindings = _where(start_date, end_date, user)

    if group_by in _GROUP_COLUMNS:
        col = _GROUP_COLUMNS[group_by]
        sql = f"""
            SELECT {col} AS {group_by},
                   COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM audit_logs {where}
            GROUP BY {col}
            ORDER BY prompt_tokens + completion_tokens DESC, {col}
        """
    else:
        sql = f"""
            SELECT substr(timestamp, 1, 10) AS date,
                   COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens
            FROM audit_logs {where}
            GROUP BY date
            ORDER BY date
        """
    return _query(sql, bindings)


def cache_usage(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
) -> list[dict]:
    """Daily prompt-cache token usage and hit ratio."""
    where, bindings = _where(start_date, end_date, user)
    sql = f"""
        SELECT substr(timestamp, 1, 10) AS date,
               COUNT(*) AS requests,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens
        FROM audit_logs {where}
        GROUP BY date
        ORDER BY date
    """
    rows = _query(sql, bindings)
    for row in rows:
        denominator = (
            row["prompt_tokens"]
            + row["cache_read_tokens"]
            + row["cache_creation_tokens"]
        )
        row["hit_ratio"] = (
            row["cache_read_tokens"] / denominator if denominator > 0 else 0.0
        )
    return rows


def request_stats(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
) -> list[dict]:
    """Daily request volume, error count and average latency."""
    where, bindings = _where(start_date, end_date, user)
    sql = f"""
        SELECT substr(timestamp, 1, 10) AS date,
               COUNT(*) AS requests,
               SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors,
               AVG(duration_ms) AS avg_duration_ms
        FROM audit_logs {where}
        GROUP BY date
        ORDER BY date
    """
    rows = _query(sql, bindings)
    for row in rows:
        row["avg_duration_ms"] = round(row["avg_duration_ms"] or 0.0, 2)
    return rows


def top_models(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Top models ranked by total (prompt + completion) tokens."""
    where, bindings = _where(start_date, end_date, user)
    limit = min(max(1, limit), 100)
    sql = f"""
        SELECT model,
               COUNT(*) AS requests,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(prompt_tokens), 0)
                 + COALESCE(SUM(completion_tokens), 0) AS total_tokens,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens
        FROM audit_logs {where + ' AND' if where else 'WHERE'} model IS NOT NULL
        GROUP BY model
        ORDER BY total_tokens DESC, requests DESC
        LIMIT ?
    """
    return _query(sql, [*bindings, limit])
