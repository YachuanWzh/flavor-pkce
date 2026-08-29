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
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
            FROM audit_logs {where}
            GROUP BY {col}
            ORDER BY prompt_tokens + completion_tokens
                       + cache_read_tokens + cache_creation_tokens DESC, {col}
        """
    else:
        sql = f"""
            SELECT substr(timestamp, 1, 10) AS date,
                   COUNT(*) AS requests,
                   COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
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


_PERCENTILES = {"p50": 0.50, "p95": 0.95, "p99": 0.99}


def _ranked_cte(where: str, partition_by_date: bool) -> str:
    if partition_by_date:
        return f"""
            WITH ranked AS (
                SELECT substr(timestamp, 1, 10) AS date, duration_ms,
                       ROW_NUMBER() OVER (
                           PARTITION BY substr(timestamp, 1, 10)
                           ORDER BY duration_ms
                       ) AS rn,
                       COUNT(*) OVER (
                           PARTITION BY substr(timestamp, 1, 10)
                       ) AS part_total
                FROM audit_logs {where}
            )
        """
    return f"""
        WITH ranked AS (
            SELECT duration_ms,
                   ROW_NUMBER() OVER (ORDER BY duration_ms) AS rn,
                   COUNT(*) OVER () AS part_total
            FROM audit_logs {where}
        )
    """


def _percentile_columns() -> str:
    """SQL select list computing p50/p95/p99 over the `ranked` CTE.

    ``MIN(CASE WHEN rn >= ceil(part_total*pct) THEN duration_ms END)``
    returns the row at rank ceil(part_total*pct): within one partition
    ``part_total`` is constant, so filtering on the rank picks exactly
    that ordered value. SQLite lacks ceil(); ``x > CAST(x AS INTEGER)``
    is its 0/1 fraction test.
    """
    parts = []
    for name, pct in _PERCENTILES.items():
        frac = f"part_total * {pct}"
        ceil_expr = f"CAST({frac} AS INTEGER) + ({frac} > CAST({frac} AS INTEGER))"
        parts.append(
            f"MIN(CASE WHEN rn >= {ceil_expr} THEN duration_ms END) AS {name}"
        )
    return ",\n               ".join(parts)


def request_stats(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
) -> list[dict]:
    """Daily request volume, error count, and avg/p50/p95/p99 latency.

    Average alone is misleading for LLM traffic (long agentic streams
    dominate it), so each day also carries approximate latency
    percentiles computed with SQLite window functions.
    """
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

    pct_sql = _ranked_cte(where, partition_by_date=True) + f"""
        SELECT date,
               {_percentile_columns()}
        FROM ranked
        GROUP BY date
    """
    percentiles = {row["date"]: row for row in _query(pct_sql, bindings)}
    for row in rows:
        pct = percentiles.get(row["date"]) or {}
        for name in _PERCENTILES:
            value = pct.get(name)
            row[name] = round(value, 2) if value is not None else 0.0
    return rows


def latency_summary(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
) -> dict:
    """Whole-period latency distribution (one row) for the dashboard card."""
    where, bindings = _where(start_date, end_date, user)
    base = _query(
        f"""SELECT COUNT(*) AS requests,
                   AVG(duration_ms) AS avg_duration_ms,
                   MAX(duration_ms) AS max_duration_ms
            FROM audit_logs {where}""",
        bindings,
    )[0]
    pct_sql = _ranked_cte(where, partition_by_date=False) + f"""
        SELECT {_percentile_columns()} FROM ranked
    """
    pct = _query(pct_sql, bindings)[0] if base["requests"] else {}
    return {
        "requests": base["requests"],
        "avg_duration_ms": round(base["avg_duration_ms"] or 0.0, 2),
        "max_duration_ms": round(base["max_duration_ms"] or 0.0, 2),
        "p50": round(pct.get("p50") or 0.0, 2),
        "p95": round(pct.get("p95") or 0.0, 2),
        "p99": round(pct.get("p99") or 0.0, 2),
    }


_ERRORS_GROUP_COLUMNS = {
    "status": "status",
    "model": 'COALESCE(model, \'(unknown)\')',
}


def errors_breakdown(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
    group_by: str = "status",
) -> list[dict]:
    """Error rows (status >= 400) grouped by status code or model."""
    column = _ERRORS_GROUP_COLUMNS.get(group_by, _ERRORS_GROUP_COLUMNS["status"])
    where, bindings = _where(start_date, end_date, user)
    where_errors = f"{where} AND" if where else "WHERE"
    sql = f"""
        SELECT {column} AS key, COUNT(*) AS count
        FROM audit_logs {where_errors} status >= 400
        GROUP BY {column}
        ORDER BY count DESC
        LIMIT 20
    """
    return _query(sql, bindings)


def _row_cost(row: dict, price: dict) -> float:
    """USD cost of one aggregated row using the model's per-1M-token prices."""
    return (
        row["prompt_tokens"] * price.get("prompt", 0.0)
        + row["completion_tokens"] * price.get("completion", 0.0)
        + row["cache_read_tokens"] * price.get("cache_read", 0.0)
        + row["cache_creation_tokens"] * price.get("cache_creation", 0.0)
    ) / 1_000_000


def cost_usage(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
    group_by: str | None = None,
) -> list[dict]:
    """Estimated USD spend, priced per model via ``prices.effective_prices``.

    Without ``group_by`` the result is a daily cost series; with
    ``group_by`` (``user`` / ``model`` / ``service``) it is a cost
    leaderboard. Models without a configured price contribute zero.
    """
    where, bindings = _where(start_date, end_date, user)
    from gateway.prices import effective_prices
    prices = effective_prices()

    if group_by in _GROUP_COLUMNS:
        group_expr = _GROUP_COLUMNS[group_by]
        label = group_by
    else:
        group_expr = "substr(timestamp, 1, 10)"
        label = "date"

    sql = f"""
        SELECT {group_expr} AS {label},
               model,
               COUNT(*) AS requests,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens
        FROM audit_logs {where}
        GROUP BY {group_expr}, model
        ORDER BY {label}
    """
    raw = _query(sql, bindings)

    aggregated: dict[str, dict] = {}
    for row in raw:
        key = row[label]
        entry = aggregated.setdefault(key, {"requests": 0, "cost": 0.0})
        entry["requests"] += row["requests"]
        entry["cost"] += _row_cost(row, prices.get(row["model"]) or {})

    items = [
        {label: key, **entry}
        for key, entry in sorted(
            aggregated.items(), key=lambda kv: kv[1]["cost"], reverse=True,
        )
    ]
    for item in items:
        item["cost"] = round(item["cost"], 6)
    return items


def top_models(
    start_date: str | None = None,
    end_date: str | None = None,
    user: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Top models ranked by total tokens (prompt + completion + cache)."""
    where, bindings = _where(start_date, end_date, user)
    limit = min(max(1, limit), 100)
    sql = f"""
        SELECT model,
               COUNT(*) AS requests,
               COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
               COALESCE(SUM(prompt_tokens), 0)
                 + COALESCE(SUM(completion_tokens), 0)
                 + COALESCE(SUM(cache_read_tokens), 0)
                 + COALESCE(SUM(cache_creation_tokens), 0) AS total_tokens,
               COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens
        FROM audit_logs {where + ' AND' if where else 'WHERE'} model IS NOT NULL
        GROUP BY model
        ORDER BY total_tokens DESC, requests DESC
        LIMIT ?
    """
    return _query(sql, [*bindings, limit])
