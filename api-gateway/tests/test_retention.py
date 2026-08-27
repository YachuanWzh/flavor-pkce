"""Audit retention purge + configurable body truncation (gateway.retention).

Mirrors auth-server's AUDIT_RETENTION_DAYS policy: gateway audit rows
older than the cutoff (audit_logs, agent_queries, quota_usage) are deleted
on startup.  The body truncation limit for newly written audit rows is
configurable via AUDIT_BODY_MAX_CHARS; 0 disables storing bodies at all.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

import gateway.config as config
from gateway.database import (
    init_audit_db, insert_log, insert_agent_query, query_logs, verify_integrity,
)
from gateway.retention import purge_old_logs, truncate_body


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="retention_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    init_audit_db()
    monkeypatch.setattr(config, "AUDIT_RETENTION_DAYS", 30, raising=False)
    monkeypatch.setattr(config, "AUDIT_BODY_MAX_CHARS", 50000, raising=False)
    yield
    config.AUDIT_DB_PATH = old
    if os.path.exists(tmp):
        os.remove(tmp)


def _iso(days_ago: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


def _insert_audit(timestamp: str, user="alice", path="/v1/chat"):
    insert_log(
        timestamp=timestamp, user=user, method="POST", path=path,
        status=200, duration_ms=10.0, upstream_ms=None, level="INFO",
    )


# ---------------------------------------------------------------------------
# purge_old_logs
# ---------------------------------------------------------------------------

def test_purge_removes_only_expired_audit_logs():
    _insert_audit(_iso(40))   # expired
    _insert_audit(_iso(45))   # expired
    _insert_audit(_iso(5))    # fresh
    deleted = purge_old_logs()
    assert deleted == 2
    result = query_logs({"page": 1, "page_size": 10})
    assert result["total"] == 1


def test_purge_removes_expired_agent_queries():
    insert_agent_query(
        timestamp=_iso(40), user="alice", question="old", status="success",
        duration_ms=1.0,
    )
    insert_agent_query(
        timestamp=_iso(1), user="alice", question="new", status="success",
        duration_ms=1.0,
    )
    purge_old_logs()
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    rows = conn.execute("SELECT question FROM agent_queries").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["new"]


def test_purge_removes_stale_quota_usage_rows():
    from gateway.quota import record_usage
    record_usage("alice", prompt_tokens=10, completion_tokens=5,
                 day="2020-01-01")
    record_usage("alice", prompt_tokens=10, completion_tokens=5,
                 day="2099-01-01")
    purge_old_logs()
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    days = [r[0] for r in conn.execute("SELECT day FROM quota_usage").fetchall()]
    conn.close()
    assert days == ["2099-01-01"]


def test_purge_disabled_when_retention_zero(monkeypatch):
    monkeypatch.setattr(config, "AUDIT_RETENTION_DAYS", 0, raising=False)
    _insert_audit(_iso(999))
    assert purge_old_logs() == 0
    assert query_logs({"page": 1, "page_size": 10})["total"] == 1


def test_startup_purges_expired_logs():
    """The app lifespan runs the retention purge when the gateway boots."""
    from fastapi.testclient import TestClient
    import gateway.main as gm

    _insert_audit(_iso(40))  # expired
    _insert_audit(_iso(2))   # fresh
    with TestClient(gm.app):
        pass
    assert query_logs({"page": 1, "page_size": 10})["total"] == 1


def test_hash_chain_still_valid_after_purge():
    _insert_audit(_iso(40))
    _insert_audit(_iso(5))
    _insert_audit(_iso(4))
    purge_old_logs()
    assert verify_integrity() is True


# ---------------------------------------------------------------------------
# truncate_body (configurable limit, shared by insert_log)
# ---------------------------------------------------------------------------

def test_truncate_body_keeps_short_text(monkeypatch):
    monkeypatch.setattr(config, "AUDIT_BODY_MAX_CHARS", 100, raising=False)
    assert truncate_body("hello") == "hello"


def test_truncate_body_clips_and_marks(monkeypatch):
    monkeypatch.setattr(config, "AUDIT_BODY_MAX_CHARS", 10, raising=False)
    out = truncate_body("x" * 25)
    assert out.startswith("x" * 10)
    assert out.endswith("[truncated]")


def test_truncate_body_zero_disables_storage(monkeypatch):
    monkeypatch.setattr(config, "AUDIT_BODY_MAX_CHARS", 0, raising=False)
    assert truncate_body("x" * 100) is None


def test_truncate_body_none_passthrough():
    assert truncate_body(None) is None


def test_insert_log_uses_configurable_body_limit(monkeypatch):
    # Regression: insert_log must honour AUDIT_BODY_MAX_CHARS, not a constant.
    monkeypatch.setattr(config, "AUDIT_BODY_MAX_CHARS", 5, raising=False)
    insert_log(
        timestamp=_iso(0), user="alice", method="POST", path="/v1/chat",
        status=200, duration_ms=1.0, upstream_ms=None, level="INFO",
        request_body="abcdefghij",
    )
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    row = conn.execute("SELECT request_body FROM audit_logs").fetchone()[0]
    conn.close()
    assert row.startswith("abcde")
    assert row.endswith("[truncated]")
