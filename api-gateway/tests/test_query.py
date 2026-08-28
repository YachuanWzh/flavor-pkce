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


def test_with_cte_query_allowed():
    """WITH ... SELECT (CTEs) must be accepted; CTE names are not real
    tables and must not hit the allowlist check."""
    result = execute_readonly_query(
        "WITH per_user AS (SELECT \"user\", "
        "SUM(prompt_tokens + completion_tokens) AS total "
        "FROM v_audit_agent GROUP BY \"user\") "
        "SELECT * FROM per_user ORDER BY total DESC"
    )
    assert result["columns"] == ["user", "total"]
    assert len(result["rows"]) == 2
    assert result["rows"][0]["user"] == "alice"
    assert result["rows"][0]["total"] == 230


def test_with_cte_denied_table_inside_body_rejected():
    """A CTE reading a non-whitelisted table is still denied (authorizer
    sees the base-table read)."""
    with pytest.raises(ValueError, match="not allowed"):
        execute_readonly_query(
            "WITH c AS (SELECT name FROM sqlite_master) SELECT * FROM c"
        )


def test_with_cte_column_list_and_inner_limit():
    """`WITH name(cols) AS (...)` is recognised, and an inner LIMIT does
    not stop the outer row cap from being appended."""
    result = execute_readonly_query(
        "WITH ranked(u) AS (SELECT \"user\" FROM v_audit_agent "
        "GROUP BY \"user\" ORDER BY COUNT(*) DESC LIMIT 1) "
        "SELECT * FROM ranked"
    )
    assert "u" in result["columns"]
    assert len(result["rows"]) == 1


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


def test_from_subquery_not_mistaken_as_table():
    """`FROM (SELECT ...)` must not be parsed as a table named "select"."""
    result = execute_readonly_query(
        "SELECT * FROM (SELECT \"user\" AS u FROM v_audit_agent)"
    )
    assert len(result["rows"]) == 2


def test_from_subquery_denied_table_still_rejected():
    with pytest.raises(ValueError, match="not allowed"):
        execute_readonly_query(
            "SELECT * FROM (SELECT name FROM sqlite_master)"
        )


def test_total_tokens_null_semantics_match_dashboard():
    """Regression: NULL cache columns (rows predating the cache columns or
    without usage data) must not drop the row's prompt/completion from the
    total. The dashboard sums each column separately (per-column SUM
    ignores NULLs); per-row `SUM(a+b+c+d)` yields NULL and silently
    undercounts."""
    per_column = execute_readonly_query(
        "SELECT COALESCE(SUM(prompt_tokens), 0) + COALESCE(SUM(completion_tokens), 0)"
        " + COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_creation_tokens), 0)"
        " AS t FROM v_audit_agent"
    )
    assert per_column["rows"][0]["t"] == 310  # 230 (alice) + 80 (bob)

    per_row = execute_readonly_query(
        "SELECT SUM(prompt_tokens + completion_tokens + cache_read_tokens"
        " + cache_creation_tokens) AS t FROM v_audit_agent"
    )
    # Both fixture rows have NULL cache columns → the naive per-row sum is NULL.
    assert per_row["rows"][0]["t"] is None


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


def test_agent_view_created_and_queryable():
    """The agent-safe view (no request/response body columns) must exist and
    be queryable through the executor."""
    result = execute_readonly_query("SELECT COUNT(*) AS n FROM v_audit_agent")
    assert result["rows"][0]["n"] == 2


def test_sensitive_body_columns_denied_in_agent_mode():
    """Regression (P0-1): the data agent must not be able to read prompt/response
    bodies even though audit_logs itself stays in the allowlist for the
    administrator /api/query endpoint."""
    # Default mode keeps body columns readable for administrators.
    result = execute_readonly_query(
        "SELECT request_body FROM audit_logs LIMIT 1",
    )
    assert "request_body" in result["columns"]

    # Agent mode denies the columns at the engine level.
    with pytest.raises(ValueError, match="prohibited|not allowed"):
        execute_readonly_query(
            "SELECT request_body FROM audit_logs LIMIT 1",
            allow_sensitive_columns=False,
        )
    # SELECT * expands to the sensitive columns and must also be denied.
    with pytest.raises(ValueError, match="prohibited|not allowed"):
        execute_readonly_query(
            "SELECT * FROM audit_logs LIMIT 1",
            allow_sensitive_columns=False,
        )


def test_agent_mode_denies_bodies_even_with_alias():
    """Aliasing must not bypass the agent-mode body-column ban."""
    with pytest.raises(ValueError, match="prohibited|not allowed"):
        execute_readonly_query(
            "SELECT request_body AS rb FROM audit_logs LIMIT 1",
            allow_sensitive_columns=False,
        )
