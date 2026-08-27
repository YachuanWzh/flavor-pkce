"""SQL safety guard tests (agent-loop Task 1)."""

import pytest

from gateway.sqlguard import check_sql_safety


def test_safe_select_passes():
    ok, reason = check_sql_safety(
        'SELECT "user", COUNT(*) AS n FROM audit_logs GROUP BY "user"'
    )
    assert ok is True
    assert reason is None


def test_safe_select_from_view_passes():
    ok, reason = check_sql_safety("SELECT * FROM v_audit_daily LIMIT 5")
    assert ok is True
    assert reason is None


@pytest.mark.parametrize("sql", [
    "INSERT INTO audit_logs VALUES (1)",
    "UPDATE audit_logs SET user = 'x'",
    "DELETE FROM audit_logs",
    "DROP TABLE audit_logs",
    "CREATE TABLE evil (id INT)",
    "ALTER TABLE audit_logs ADD COLUMN x TEXT",
    "PRAGMA table_info(audit_logs)",
    "ATTACH DATABASE 'x.db' AS evil",
    "VACUUM",
    "REINDEX",
    "WITH evil AS (SELECT 1) DELETE FROM audit_logs",
])
def test_write_statements_blocked(sql):
    ok, reason = check_sql_safety(sql)
    assert ok is False
    assert reason


def test_must_start_with_select():
    ok, reason = check_sql_safety("EXPLAIN SELECT 1")
    assert ok is False
    assert "SELECT" in reason


def test_multi_statement_blocked():
    ok, reason = check_sql_safety("SELECT 1; DROP TABLE audit_logs")
    assert ok is False
    assert reason


def test_semicolon_in_string_literal_allowed():
    ok, reason = check_sql_safety(
        "SELECT 1 AS n FROM audit_logs WHERE path = '/a;b'"
    )
    assert ok is True


def test_comment_hidden_drop_blocked():
    ok, reason = check_sql_safety("SELECT 1 /* DROP TABLE x */")
    # Comments are stripped before scanning; DROP inside a comment must not
    # execute, and stripping leaves a harmless single SELECT.
    assert ok is True


def test_comment_hidden_drop_outside_stripped():
    ok, reason = check_sql_safety("SELECT 1 --\nDROP TABLE audit_logs")
    assert ok is False
    assert reason


def test_empty_sql_blocked():
    ok, reason = check_sql_safety("   ")
    assert ok is False
    assert reason
