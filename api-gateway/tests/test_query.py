"""Read-only SQL query executor tests (data agent backend)."""

import os
import sqlite3
import tempfile

import pytest

import gateway.config as config
from gateway.database import init_audit_db, insert_log
from gateway.query import execute_readonly_query


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="query_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    init_audit_db()
    insert_log(
        timestamp="2026-08-01T10:00:00+00:00",
        user="alice", method="POST", path="/v1/messages",
        status=200, duration_ms=500.0, upstream_ms=450.0, level="INFO",
        prompt_tokens=150, completion_tokens=80, model="claude-sonnet-4-5",
    )
    insert_log(
        timestamp="2026-08-02T10:00:00+00:00",
        user="bob", method="POST", path="/v1/chat/completions",
        status=200, duration_ms=300.0, upstream_ms=250.0, level="INFO",
        prompt_tokens=60, completion_tokens=20, model="gpt-5",
    )
    yield
    config.AUDIT_DB_PATH = old
    if os.path.exists(tmp):
        os.remove(tmp)


def test_select_allowed():
    result = execute_readonly_query("SELECT * FROM audit_logs")
    assert result["columns"] == ["id", "timestamp", "user", "method", "path", "status", "duration_ms", "upstream_ms", "level", "prompt_tokens", "completion_tokens", "model", "session_id", "cache_read_tokens", "cache_creation_tokens", "service_name", "user_id", "client_id", "request_body", "response_body", "prev_hash", "hash"]
    assert len(result["rows"]) == 2
    assert result["rows"][0]["user"] == "alice"


def test_aggregate_query_allowed():
    result = execute_readonly_query("SELECT \"user\", COUNT(*) AS n FROM audit_logs GROUP BY \"user\" ORDER BY n DESC")
    assert len(result["rows"]) == 2
    assert result["rows"][0]["n"] == 1


def test_limit_cap_enforced():
    # Seed enough rows to exceed the hard cap; an explicit huge LIMIT is
    # clamped to MAX_ROWS (1000) and the result is flagged truncated.
    for i in range(1100):
        insert_log(
            timestamp=f"2026-08-03T10:{i % 60:02d}:00+00:00",
            user=f"u{i}", method="GET", path="/v1/models",
            status=200, duration_ms=1.0, upstream_ms=None, level="INFO",
        )
    result = execute_readonly_query("SELECT * FROM audit_logs LIMIT 5000")
    assert len(result["rows"]) == 1000
    assert result["truncated"] is True


def test_write_statements_rejected():
    with pytest.raises(ValueError, match="Only SELECT"):
        execute_readonly_query("DELETE FROM audit_logs")


def test_multi_statement_rejected():
    with pytest.raises(ValueError, match="single statement"):
        execute_readonly_query("SELECT * FROM audit_logs; DROP TABLE audit_logs")


def test_non_whitelist_table_rejected():
    with pytest.raises(ValueError, match="not allowed"):
        execute_readonly_query("SELECT * FROM sqlite_master")


def test_comma_joined_non_whitelist_table_rejected():
    # Regression: comma-separated FROM list used to bypass the allowlist.
    with pytest.raises(ValueError, match="not allowed"):
        execute_readonly_query("SELECT name FROM audit_logs, sqlite_master")


def test_subquery_non_whitelist_table_rejected():
    with pytest.raises(ValueError, match="not allowed"):
        execute_readonly_query("SELECT * FROM audit_logs WHERE id IN (SELECT id FROM sqlite_master)")


def test_join_non_whitelist_table_rejected():
    with pytest.raises(ValueError, match="not allowed"):
        execute_readonly_query("SELECT * FROM audit_logs JOIN sqlite_master ON 1=1")


def test_syntax_error_raises_valuable_error():
    # sqlite3.OperationalError for malformed SQL should surface as a clear
    # error the API layer can map to a 400, not an unhandled 500.
    with pytest.raises(sqlite3.Error):
        execute_readonly_query("SELECT * FROM")


def test_view_schema_and_aggregates():
    from gateway.database import _connect
    conn = _connect()
    rows = conn.execute(
        "SELECT date, requests, errors, prompt_tokens, completion_tokens, "
        "users, models FROM v_audit_daily ORDER BY date"
    ).fetchall()
    conn.close()
    assert len(rows) >= 1
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-08-01"]["requests"] == 1
    assert by_date["2026-08-01"]["errors"] == 0
    assert by_date["2026-08-01"]["prompt_tokens"] == 150
    assert by_date["2026-08-01"]["completion_tokens"] == 80
    assert by_date["2026-08-01"]["users"] == 1
    assert by_date["2026-08-01"]["models"] == 1


def test_view_queryable_through_executor():
    result = execute_readonly_query(
        "SELECT COUNT(*) AS n FROM v_audit_daily"
    )
    assert result["rows"][0]["n"] >= 1
