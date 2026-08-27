"""SQLite audit-log storage for the API Gateway.

Provides helpers to initialise the schema, insert log entries, and query
them with date/keyword filtering and pagination.
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import TypedDict

import gateway.config

# Maximum characters stored per body field to prevent unbounded DB growth.
_BODY_MAX_CHARS = 50_000


def _row_hash(
    prev_hash: str,
    *,
    timestamp: str, user: str, method: str, path: str, status: int,
    duration_ms: float, upstream_ms: float | None, level: str,
    prompt_tokens: int | None, completion_tokens: int | None, model: str | None,
    session_id: str | None, request_body: str | None, response_body: str | None,
) -> str:
    """SHA-256 of the previous hash plus this row's canonical content.

    The report columns added later (``cache_read_tokens``,
    ``cache_creation_tokens``, ``service_name``, ``user_id``, ``client_id``) are
    intentionally excluded: they are analytical fields, and hashing them
    would invalidate chains written before the columns existed.
    """
    payload = json.dumps(
        [timestamp, user, method, path, status, duration_ms, upstream_ms, level,
         prompt_tokens, completion_tokens, model, session_id,
         request_body, response_body],
        sort_keys=True, default=str, ensure_ascii=False,
    )
    return hashlib.sha256(f"{prev_hash}|{payload}".encode("utf-8")).hexdigest()


def _truncate_body(text: str | None) -> str | None:
    """Truncate body text to _BODY_MAX_CHARS, appending a marker if clipped."""
    if text is None:
        return None
    if len(text) <= _BODY_MAX_CHARS:
        return text
    return text[:_BODY_MAX_CHARS] + "\n…[truncated]"


def _connect() -> sqlite3.Connection:
    """Return a new connection with row-factory enabled."""
    conn = sqlite3.connect(gateway.config.AUDIT_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_audit_db() -> None:
    """Create the audit_logs table and indexes if they don't exist."""
    Path(gateway.config.AUDIT_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp         TEXT    NOT NULL,
            "user"            TEXT    NOT NULL,
            method            TEXT    NOT NULL,
            path              TEXT    NOT NULL,
            status            INTEGER NOT NULL,
            duration_ms       REAL    NOT NULL,
            upstream_ms       REAL,
            level             TEXT    NOT NULL,
            prompt_tokens     INTEGER,
            completion_tokens INTEGER,
            model             TEXT,
            session_id        TEXT,
            cache_read_tokens     INTEGER,
            cache_creation_tokens INTEGER,
            service_name          TEXT,
            user_id               TEXT,
            client_id             TEXT,
            request_body      TEXT,
            response_body     TEXT,
            prev_hash         TEXT,
            hash              TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_user      ON audit_logs("user");
        CREATE INDEX IF NOT EXISTS idx_audit_path      ON audit_logs(path);

        CREATE TABLE IF NOT EXISTS agent_queries (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp      TEXT    NOT NULL,
            "user"         TEXT    NOT NULL,
            user_id        TEXT,
            question       TEXT    NOT NULL,
            sql            TEXT,
            error          TEXT,
            rows_returned  INTEGER,
            duration_ms    REAL    NOT NULL,
            prompt_tokens  INTEGER,
            completion_tokens INTEGER,
            model          TEXT,
            status         TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS metric_terms (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            term       TEXT    NOT NULL UNIQUE,
            definition TEXT    NOT NULL,
            synonyms   TEXT    NOT NULL DEFAULT '[]',
            enabled    INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT    NOT NULL
        );
    """)
    # Migrate existing databases that lack newer columns.
    for col, col_type in (
        ("prompt_tokens", "INTEGER"),
        ("completion_tokens", "INTEGER"),
        ("model", "TEXT"),
        ("session_id", "TEXT"),
        ("cache_read_tokens", "INTEGER"),
        ("cache_creation_tokens", "INTEGER"),
        ("service_name", "TEXT"),
        ("user_id", "TEXT"),
        ("client_id", "TEXT"),
        ("request_body", "TEXT"),
        ("response_body", "TEXT"),
        ("prev_hash", "TEXT"),
        ("hash", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Ensure indexes for optional columns exist (only after columns are guaranteed to exist).
    for idx_name, idx_col in (
        ("idx_audit_model", "model"),
        ("idx_audit_session", "session_id"),
        ("idx_audit_user_id", "user_id"),
        ("idx_audit_client_id", "client_id"),
    ):
        try:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON audit_logs({idx_col})")
        except sqlite3.OperationalError:
            pass

    # Views and agent-queries indexes are created after the column migration so
    # that existing databases (missing newer columns) upgrade cleanly instead
    # of failing at CREATE VIEW.
    conn.executescript("""
        CREATE VIEW IF NOT EXISTS v_audit_daily AS
        SELECT
            substr(timestamp, 1, 10) AS date,
            COUNT(*) AS requests,
            SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors,
            ROUND(AVG(duration_ms), 2) AS avg_duration_ms,
            COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
            COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
            COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
            COUNT(DISTINCT "user") AS users,
            COUNT(DISTINCT model) AS models
        FROM audit_logs
        GROUP BY date;

        CREATE VIEW IF NOT EXISTS v_audit_agent AS
        SELECT
            id, timestamp, "user", method, path, status, duration_ms,
            upstream_ms, level, prompt_tokens, completion_tokens, model,
            session_id, cache_read_tokens, cache_creation_tokens,
            service_name, user_id, client_id
        FROM audit_logs;

        CREATE INDEX IF NOT EXISTS idx_agent_queries_timestamp
            ON agent_queries(timestamp);
        CREATE INDEX IF NOT EXISTS idx_agent_queries_user
            ON agent_queries("user");
    """)

    conn.commit()
    conn.close()


def insert_log(
    *,
    timestamp: str,
    user: str,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    upstream_ms: float | None,
    level: str,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    model: str | None = None,
    session_id: str | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    service_name: str | None = None,
    user_id: str | None = None,
    client_id: str | None = None,
    request_body: str | None = None,
    response_body: str | None = None,
) -> None:
    """Insert a single audit-log row.

    Body fields are truncated to ``_BODY_MAX_CHARS`` to prevent unbounded
    storage growth from large LLM payloads. Each row is chained to the
    previous row via ``prev_hash``/``hash`` (tamper-evident audit trail).
    """
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE")
    last = conn.execute(
        "SELECT hash FROM audit_logs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    prev_hash = last["hash"] if last and last["hash"] is not None else ""

    request_body = _truncate_body(request_body)
    response_body = _truncate_body(response_body)
    row_hash = _row_hash(
        prev_hash,
        timestamp=timestamp, user=user, method=method, path=path, status=status,
        duration_ms=duration_ms, upstream_ms=upstream_ms, level=level,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        model=model, session_id=session_id,
        request_body=request_body, response_body=response_body,
    )

    conn.execute(
        """INSERT INTO audit_logs
           (timestamp, "user", method, path, status, duration_ms,
            upstream_ms, level, prompt_tokens, completion_tokens, model,
            session_id, cache_read_tokens, cache_creation_tokens,
            service_name, user_id, client_id, request_body, response_body,
            prev_hash, hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (timestamp, user, method, path, status, duration_ms,
         upstream_ms, level, prompt_tokens, completion_tokens, model,
         session_id, cache_read_tokens, cache_creation_tokens,
         service_name, user_id, client_id, request_body, response_body,
         prev_hash, row_hash),
    )
    conn.commit()
    conn.close()


def verify_integrity() -> bool:
    """Re-verify the whole hash chain. False means the log was tampered with."""
    conn = _connect()
    try:
        prev = ""
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY id")
        for row in rows:
            row_prev = row["prev_hash"] or ""
            if row_prev != prev:
                return False
            expected = _row_hash(
                prev,
                timestamp=row["timestamp"], user=row["user"], method=row["method"],
                path=row["path"], status=row["status"], duration_ms=row["duration_ms"],
                upstream_ms=row["upstream_ms"], level=row["level"],
                prompt_tokens=row["prompt_tokens"], completion_tokens=row["completion_tokens"],
                model=row["model"], session_id=row["session_id"],
                request_body=row["request_body"], response_body=row["response_body"],
            )
            if row["hash"] != expected:
                return False
            prev = row["hash"]
        return True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

class QueryParams(TypedDict, total=False):
    page: int
    page_size: int
    start_date: str | None
    end_date: str | None
    keyword: str | None
    user: str | None


class PageResult(TypedDict):
    total: int
    page: int
    page_size: int
    items: list[dict]


def query_logs(params: QueryParams) -> PageResult:
    """Return a paginated page of audit-log entries.

    Parameters
    ----------
    params :
        ``page``        — 1-based page number (default 1).
        ``page_size``   — entries per page (default 20, max 200).
        ``start_date``  — ISO date string (inclusive), e.g. ``"2026-07-01"``.
        ``end_date``    — ISO date string (inclusive), e.g. ``"2026-07-22"``.
        ``keyword``     — case-insensitive substring match across common audit fields.
        ``user``        — exact match on ``user``.
    """
    page = max(1, params.get("page", 1))
    page_size = min(max(1, params.get("page_size", 20)), 200)

    conditions: list[str] = []
    bindings: list[object] = []

    if params.get("start_date"):
        conditions.append("timestamp >= ?")
        bindings.append(params["start_date"] + "T00:00:00+00:00")

    if params.get("end_date"):
        conditions.append("timestamp <= ?")
        bindings.append(params["end_date"] + "T23:59:59.999999+00:00")

    if params.get("keyword"):
        kw = params["keyword"]
        conditions.append(
            '("user" LIKE ? OR path LIKE ? OR model LIKE ?'
            ' OR session_id LIKE ? OR client_id LIKE ? OR request_body LIKE ?'
            ' OR response_body LIKE ?)'
        )
        like = f"%{kw}%"
        bindings.extend([like, like, like, like, like, like, like])

    if params.get("user"):
        conditions.append('"user" = ?')
        bindings.append(params["user"])

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    order = "ORDER BY id DESC"

    # Column list for the list view — excludes heavy body fields.
    _LIST_COLS = (
        "id, timestamp, \"user\", method, path, status, duration_ms,"
        " upstream_ms, level, prompt_tokens, completion_tokens, model,"
        " session_id, cache_read_tokens, cache_creation_tokens,"
        " service_name, user_id, client_id"
    )

    conn = _connect()

    # Total count
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM audit_logs {where}", bindings
    ).fetchone()
    total = count_row[0] if count_row else 0

    # Page
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT {_LIST_COLS} FROM audit_logs {where} {order} LIMIT ? OFFSET ?",
        [*bindings, page_size, offset],
    ).fetchall()

    conn.close()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [dict(row) for row in rows],
    }


def query_log_by_id(log_id: int) -> dict | None:
    """Return a single log entry by primary key, including body columns."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM audit_logs WHERE id = ?", (log_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def clear_logs() -> int:
    """Delete all rows from the audit_logs table. Returns deleted count."""
    conn = _connect()
    count = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    conn.execute("DELETE FROM audit_logs")
    conn.commit()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# Data-agent query audit (P0-1)
# ---------------------------------------------------------------------------

def insert_agent_query(
    *,
    timestamp: str,
    user: str,
    question: str,
    sql: str | None = None,
    error: str | None = None,
    rows_returned: int | None = None,
    duration_ms: float,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    model: str | None = None,
    status: str,
    user_id: str | None = None,
) -> None:
    """Record one NL→SQL data-agent interaction for auditability.

    Unlike ``audit_logs`` (upstream proxy traffic, hash-chained), this
    table captures the administrator's own agent usage: the natural-language
    question, the generated SQL, success/error and upstream token usage.
    """
    conn = _connect()
    conn.execute(
        """INSERT INTO agent_queries
           (timestamp, "user", user_id, question, sql, error, rows_returned,
            duration_ms, prompt_tokens, completion_tokens, model, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (timestamp, user, user_id, question, sql, error, rows_returned,
         duration_ms, prompt_tokens, completion_tokens, model, status),
    )
    conn.commit()
    conn.close()


def query_agent_queries(
    params: QueryParams,
) -> PageResult:
    """Return a paginated page of data-agent query records."""
    page = max(1, params.get("page", 1))
    page_size = min(max(1, params.get("page_size", 20)), 200)

    conditions: list[str] = []
    bindings: list[object] = []
    if params.get("start_date"):
        conditions.append("timestamp >= ?")
        bindings.append(params["start_date"] + "T00:00:00+00:00")
    if params.get("end_date"):
        conditions.append("timestamp <= ?")
        bindings.append(params["end_date"] + "T23:59:59.999999+00:00")
    if params.get("user"):
        conditions.append('"user" = ?')
        bindings.append(params["user"])
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = _connect()
    count_row = conn.execute(
        f"SELECT COUNT(*) FROM agent_queries {where}", bindings,
    ).fetchone()
    total = count_row[0] if count_row else 0

    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM agent_queries {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*bindings, page_size, offset],
    ).fetchall()
    conn.close()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [dict(row) for row in rows],
    }


def agent_query_stats(params: QueryParams) -> dict:
    """Aggregate data-agent usage: totals, daily series, error top."""
    conditions: list[str] = []
    bindings: list[object] = []
    if params.get("start_date"):
        conditions.append("timestamp >= ?")
        bindings.append(params["start_date"] + "T00:00:00+00:00")
    if params.get("end_date"):
        conditions.append("timestamp <= ?")
        bindings.append(params["end_date"] + "T23:59:59.999999+00:00")
    if params.get("user"):
        conditions.append('"user" = ?')
        bindings.append(params["user"])
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    conn = _connect()
    try:
        row = conn.execute(
            f"""SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error,
                       SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked,
                       ROUND(AVG(CASE WHEN duration_ms > 0 THEN duration_ms END), 2) AS avg_duration_ms,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                FROM agent_queries {where}""",
            bindings,
        ).fetchone()
        daily = conn.execute(
            f"""SELECT substr(timestamp, 1, 10) AS date,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error,
                       SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked
                FROM agent_queries {where}
                GROUP BY date ORDER BY date""",
            bindings,
        ).fetchall()
        if where:
            error_rows = conn.execute(
                f"""SELECT COALESCE(error, 'unknown') AS error, COUNT(*) AS count
                    FROM agent_queries {where} AND error IS NOT NULL
                    GROUP BY error ORDER BY count DESC LIMIT 10""",
                bindings,
            ).fetchall()
        else:
            error_rows = conn.execute(
                """SELECT COALESCE(error, 'unknown') AS error, COUNT(*) AS count
                   FROM agent_queries WHERE error IS NOT NULL
                   GROUP BY error ORDER BY count DESC LIMIT 10"""
            ).fetchall()
    finally:
        conn.close()

    total = row["total"] or 0
    return {
        "total": total,
        "success": row["success"] or 0,
        "error": row["error"] or 0,
        "blocked": row["blocked"] or 0,
        "success_rate": round((row["success"] or 0) / total, 4) if total else 0.0,
        "avg_duration_ms": row["avg_duration_ms"],
        "prompt_tokens": row["prompt_tokens"],
        "completion_tokens": row["completion_tokens"],
        "daily": [dict(r) for r in daily],
        "error_top": [dict(r) for r in error_rows],
    }
