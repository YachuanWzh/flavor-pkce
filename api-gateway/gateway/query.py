"""Read-only SQL query executor for the data agent.

Security model:
- Only a single ``SELECT`` statement is allowed (no multi-statement).
- Only tables/views in an explicit allowlist are queryable.
- A hard row cap is always applied (the caller can lower it, never raise it).
- The SQLite connection is opened in read-only URI mode with ``query_only``
  set, as defense-in-depth against any write slipping through.

The gateway always writes UTC ISO timestamps, so ``substr(timestamp, 1, 10)``
is the UTC day — the same convention used by ``gateway.stats``.
"""

import re
import sqlite3

import gateway.config

# Tables/views a data agent may read. Keep in sync with database.py schema.
ALLOWED_TABLES = frozenset({"audit_logs", "v_audit_daily"})

# Hard upper bound on returned rows (agent-friendly, bounded memory).
MAX_ROWS = 1000

# Column names reported for audit_logs (in schema order).
AUDIT_LOG_COLUMNS = (
    "id", "timestamp", "user", "method", "path", "status", "duration_ms",
    "upstream_ms", "level", "prompt_tokens", "completion_tokens", "model",
    "session_id", "cache_read_tokens", "cache_creation_tokens",
    "service_name", "user_id", "client_id", "request_body", "response_body",
    "prev_hash", "hash",
)

# Daily aggregate view (created by database.init_audit_db).
DAILY_VIEW_COLUMNS = (
    "date", "requests", "errors", "avg_duration_ms", "prompt_tokens",
    "completion_tokens", "cache_read_tokens", "cache_creation_tokens",
    "users", "models",
)

SCHEMA_DESCRIPTIONS = {
    "audit_logs": (
        "Per-request audit log: id, timestamp (ISO8601 UTC), user, method, path, "
        "status, duration_ms, upstream_ms, level, prompt_tokens, completion_tokens, "
        "model, session_id, cache_read_tokens, cache_creation_tokens, service_name, "
        "user_id, client_id, request_body, response_body, prev_hash, hash"
    ),
    "v_audit_daily": (
        "Daily aggregate view: date (YYYY-MM-DD), requests, errors (status>=400), "
        "avg_duration_ms, prompt_tokens, completion_tokens, cache_read_tokens, "
        "cache_creation_tokens, users (distinct), models (distinct)"
    ),
}

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(sqlite_|pragma|attach|detach|vacuum|reindex|create|drop|alter|"
    r"insert|update|delete|replace|truncate)\b",
    re.IGNORECASE,
)


def _extract_tables(sql: str) -> set[str]:
    """Best-effort extraction of referenced tables from a SELECT statement."""
    # Strip string literals to avoid matching table names inside quotes.
    cleaned = re.sub(r"'([^']|'')*'", "''", sql)
    cleaned = re.sub(r'"([^"]|"")*"', '""', cleaned)
    tokens = re.split(r"\s+", cleaned)
    tables: set[str] = set()
    for i, token in enumerate(tokens):
        upper = token.upper()
        if upper in ("FROM", "JOIN", "INTO", "UPDATE", "TABLE"):
            if i + 1 < len(tokens):
                name = tokens[i + 1].strip("(),;")
                if name:
                    tables.add(name.lower())
    return tables


def execute_readonly_query(sql: str, max_rows: int = MAX_ROWS) -> dict:
    """Execute a read-only SELECT against the audit database.

    Returns ``{"columns": [...], "rows": [...], "truncated": bool}`` where
    each row is a dict keyed by column name.

    Raises ``ValueError`` for disallowed statements, tables, or multi-statements.
    """
    sql = sql.strip()
    if not sql:
        raise ValueError("Empty query")
    if not sql.lower().startswith("select"):
        raise ValueError("Only SELECT statements are allowed")
    if ";" in sql.rstrip(";"):
        raise ValueError("Only a single statement is allowed")
    if _FORBIDDEN_KEYWORDS.search(sql):
        raise ValueError("Statement contains disallowed keywords")

    tables = _extract_tables(sql)
    if tables and not tables <= ALLOWED_TABLES:
        bad = sorted(tables - ALLOWED_TABLES)
        raise ValueError(f"Table/view not allowed: {', '.join(bad)}")

    # Force a row cap: if the user didn't specify a LIMIT, add one; if they
    # did, clamp it to max_rows. Rewriting is safe because we only allow a
    # single SELECT and we verified the table allowlist.
    limit = max_rows
    m = re.search(r"\blimit\s+(\d+)", sql, re.IGNORECASE)
    if m:
        requested = int(m.group(1))
        limit = min(requested, max_rows)
        sql = sql[: m.start()] + f"LIMIT {limit}" + sql[m.end():]
    else:
        sql = sql + f" LIMIT {limit}"

    conn = sqlite3.connect(
        f"file:{gateway.config.AUDIT_DB_PATH}?mode=ro", uri=True
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    truncated = len(rows) >= limit
    return {"columns": cols, "rows": rows, "truncated": truncated}
