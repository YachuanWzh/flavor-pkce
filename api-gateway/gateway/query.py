"""Read-only SQL query executor for the data agent.

Security model:
- Only a single read-only statement is allowed: ``SELECT`` optionally
  prefixed by ``WITH`` CTEs (no multi-statement, no writes).
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
ALLOWED_TABLES = frozenset({"audit_logs", "v_audit_daily", "v_audit_agent"})

# Columns that contain raw request/response bodies (prompt text, model
# replies). The administrator /api/query endpoint may read them; the data
# agent never may (P0-1).
SENSITIVE_COLUMNS = frozenset({"request_body", "response_body"})

# Hard upper bound on returned rows (agent-friendly, bounded memory).
MAX_ROWS = 1000

# Aggregation guidance shared by every schema description and the agent
# system prompts. The dashboard (gateway.stats) sums each token column
# independently — per-column SUM skips NULLs, while a per-row expression
# `SUM(a + b + c + d)` turns NULL for any row missing one column (rows
# predating the cache columns, non-LLM requests without usage data) and
# silently drops it, undercounting. Keep the agent's SQL on the dashboard
# semantics or the two numbers will never agree.
TOTAL_TOKENS_GUIDE = (
    "Total tokens: aggregate EACH column separately — COALESCE(SUM(prompt_tokens), 0) "
    "+ COALESCE(SUM(completion_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) "
    "+ COALESCE(SUM(cache_creation_tokens), 0) — matching the dashboard. Never use "
    "SUM(prompt_tokens + completion_tokens + cache_read_tokens + cache_creation_tokens): "
    "a NULL in any one column (older rows have NULL cache columns; non-LLM requests "
    "have NULL usage) NULLifies the whole row expression and the row is silently "
    "dropped. For per-row totals wrap each column: COALESCE(prompt_tokens, 0) "
    "+ COALESCE(completion_tokens, 0) + COALESCE(cache_read_tokens, 0) "
    "+ COALESCE(cache_creation_tokens, 0)."
)

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
        "user_id, client_id, request_body, response_body, prev_hash, hash. "
        "NOTE: request_body and response_body are NOT readable by the agent; "
        "use the v_audit_agent view instead. "
        + TOTAL_TOKENS_GUIDE
    ),
    "v_audit_agent": (
        "Audit log without request/response bodies: id, timestamp (ISO8601 UTC), "
        "user, method, path, status, duration_ms, upstream_ms, level, "
        "prompt_tokens, completion_tokens, model, session_id, cache_read_tokens, "
        "cache_creation_tokens, service_name, user_id, client_id. "
        + TOTAL_TOKENS_GUIDE
    ),
    "v_audit_daily": (
        "Daily aggregate view: date (YYYY-MM-DD), requests, errors (status>=400), "
        "avg_duration_ms, prompt_tokens, completion_tokens, cache_read_tokens, "
        "cache_creation_tokens, users (distinct), models (distinct). "
        + TOTAL_TOKENS_GUIDE
    ),
}

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(sqlite_|pragma|attach|detach|vacuum|reindex|create|drop|alter|"
    r"insert|update|delete|replace|truncate)\b",
    re.IGNORECASE,
)

# SQLite authorizer action codes (sqlite3.h).
_SQLITE_OK = 0
_SQLITE_DENY = 1
_SQLITE_READ = 20


def _make_authorizer(allow_sensitive_columns: bool = True):
    """Engine-level allowlist: deny reads of any non-whitelisted table.

    This is the authoritative check — it catches every way a query can
    reference a table (comma lists, subqueries, JOINs, etc.), which the
    regex-based ``_extract_tables`` heuristic cannot fully guarantee.

    When ``allow_sensitive_columns`` is False (data-agent mode, P0-1) the
    raw request/response body columns are also denied — the agent can
    aggregate on the log but can never exfiltrate prompt text.

    Note: for SQLITE_READ, Python's sqlite3 passes the *table* name as
    ``arg1`` and the *column* name as ``arg2`` (verified empirically), the
    reverse of what the C API docs suggest — the C API's (database_name,
    table_name, column_name) ordering is not preserved in the Python 5-arg
    callback for the READ action.
    """
    def _authorizer(action, arg1, arg2, arg3, arg4):
        if action == _SQLITE_READ:
            table = (arg1 or "").lower()
            if table and table not in ALLOWED_TABLES:
                return _SQLITE_DENY
            if not allow_sensitive_columns and (arg2 or "").lower() in SENSITIVE_COLUMNS:
                return _SQLITE_DENY
        return _SQLITE_OK
    return _authorizer


def _extract_tables(sql: str) -> set[str]:
    """Best-effort extraction of referenced tables from a SELECT statement."""
    # Strip string literals to avoid matching table names inside quotes.
    cleaned = re.sub(r"'([^']|'')*'", "''", sql)
    cleaned = re.sub(r'"([^"]|"")*"', '""', cleaned)
    tokens = re.split(r"\s+", cleaned)
    # A FROM/JOIN can be followed by a subquery (`FROM (SELECT`) or a
    # masked quoted identifier; those are not real table names. Base
    # tables inside the subquery are picked up by their own FROM/JOIN.
    non_tables = {"select", "with", "values", '""'}
    tables: set[str] = set()
    for i, token in enumerate(tokens):
        upper = token.upper()
        if upper in ("FROM", "JOIN", "INTO", "UPDATE", "TABLE"):
            if i + 1 < len(tokens):
                name = tokens[i + 1].strip("(),;")
                if name and name.lower() not in non_tables:
                    tables.add(name.lower())
    return tables


# A CTE definition: `WITH name AS (`, `WITH name(a, b) AS (`, or a
# comma-continuation `, name AS (`. These names shadow real tables in
# FROM/JOIN, so they must be excluded from the allowlist pre-check. The
# authorizer still guards the real base tables read inside the CTE body.
_CTE_DEFINITION = re.compile(
    r"(?:\bwith\b|,)\s+(?:recursive\s+)?([a-z_][a-z0-9_]*)\s*(?:\([^()]*\))?\s*as\s*\(",
    re.IGNORECASE,
)


def _extract_cte_names(sql: str) -> set[str]:
    """Names bound by WITH clauses (after masking string literals)."""
    cleaned = re.sub(r"'([^']|'')*'", "''", sql)
    cleaned = re.sub(r'"([^"]|"")*"', '""', cleaned)
    return {m.group(1).lower() for m in _CTE_DEFINITION.finditer(cleaned)}


# A top-level trailing LIMIT: `LIMIT n`, `LIMIT n OFFSET m`, or the
# MySQL-style `LIMIT o, c`. Only this form is clamped: an inner LIMIT (in
# a subquery or CTE body) does not bound the outer result set.
_TRAILING_LIMIT = re.compile(
    r"\blimit\s+(\d+)\s*(?:,\s*(\d+))?(\s+offset\s+\d+)?\s*$",
    re.IGNORECASE,
)


def execute_readonly_query(
    sql: str,
    max_rows: int = MAX_ROWS,
    allow_sensitive_columns: bool = True,
) -> dict:
    """Execute a read-only SELECT/WITH query against the audit database.

    Returns ``{"columns": [...], "rows": [...], "truncated": bool}`` where
    each row is a dict keyed by column name.

    ``allow_sensitive_columns=False`` (data-agent mode, P0-1) additionally
    denies any read of the request/response body columns.

    Raises ``ValueError`` for disallowed statements, tables, or multi-statements.
    """
    sql = sql.strip().rstrip(";").rstrip()
    if not sql:
        raise ValueError("Empty query")
    if not re.match(r"(select|with)\b", sql, re.IGNORECASE):
        raise ValueError("Only SELECT/WITH statements are allowed")
    if ";" in sql:
        raise ValueError("Only a single statement is allowed")
    if _FORBIDDEN_KEYWORDS.search(sql):
        raise ValueError("Statement contains disallowed keywords")

    tables = _extract_tables(sql) - _extract_cte_names(sql)
    if tables and not tables <= ALLOWED_TABLES:
        bad = sorted(tables - ALLOWED_TABLES)
        raise ValueError(f"Table/view not allowed: {', '.join(bad)}")

    # Force a row cap: clamp a top-level trailing LIMIT to max_rows; if
    # the statement has none, append one. Rewriting is safe because we
    # only allow a single read-only statement and verified the allowlist.
    limit = max_rows
    m = _TRAILING_LIMIT.search(sql)
    if m:
        if m.group(2) is not None:  # `LIMIT offset, count` — clamp count
            limit = min(int(m.group(2)), max_rows)
            replacement = f"LIMIT {m.group(1)}, {limit}"
        else:
            limit = min(int(m.group(1)), max_rows)
            replacement = f"LIMIT {limit}{m.group(3) or ''}"
        sql = sql[: m.start()] + replacement
    else:
        sql = sql + f" LIMIT {limit}"

    conn = sqlite3.connect(
        f"file:{gateway.config.AUDIT_DB_PATH}?mode=ro", uri=True
    )
    conn.row_factory = sqlite3.Row
    conn.set_authorizer(_make_authorizer(allow_sensitive_columns))
    conn.execute("PRAGMA query_only = ON")
    try:
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        except sqlite3.DatabaseError as exc:
            # Authorizer denials surface as DatabaseError; map them to the
            # same ValueError contract the API layer turns into a 400.
            # Other DB errors (e.g. SQL syntax errors) keep their type so
            # the API layer can still report them distinctly.
            if "prohibited" in str(exc).lower():
                raise ValueError(f"Table/view not allowed: {exc}") from exc
            raise
    finally:
        conn.close()

    truncated = len(rows) >= limit
    return {"columns": cols, "rows": rows, "truncated": truncated}
