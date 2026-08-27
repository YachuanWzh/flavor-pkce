"""Correction loop: rejected/corrected agent queries become Q&A knowledge.

Closes the NL→SQL feedback gap — an admin who rejects a wrong SQL (or edits
it) can store the canonical question → SQL pair with one click, so the same
class of question generates correctly next time (few-shot injection,
``gateway.qa``).
"""

import json
import os
import sqlite3
import tempfile

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_agent_query, query_agent_queries


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="correction_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    old_token = config.AUDIT_API_TOKEN
    config.AUDIT_DB_PATH = tmp
    config.AUDIT_API_TOKEN = "test-audit-token"
    init_audit_db()
    yield
    config.AUDIT_DB_PATH = old
    config.AUDIT_API_TOKEN = old_token
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.fixture(autouse=True)
def setup_keys():
    keys_dir = os.path.join(os.path.dirname(__file__), "..", "..", "auth-server", "keys")
    os.makedirs(keys_dir, exist_ok=True)
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auth-server"))
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()
    config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
    yield


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


AUTH = {"X-Audit-Token": "test-audit-token"}


def _agent_qa_questions():
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    rows = conn.execute("SELECT question, sql_template, tags FROM agent_qa_pairs").fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Recording rejections (the input side of the loop)
# ---------------------------------------------------------------------------

def test_rejected_turn_is_recorded_as_rejected_with_sql():
    """A human rejection must persist the question + the rejected SQL, else
    the review page has nothing to convert into a knowledge pair."""
    import asyncio
    from gateway.agent_loop import confirm_agent_turn
    from gateway.session import SessionStore

    store = SessionStore(ttl_seconds=60.0, max_sessions=4)
    session = store.create(user_id="u1")
    session.user = "admin"
    session.pending_sql = "SELECT bad()"
    session.pending_question = "上月销售额"

    async def _consume():
        return [e async for e in confirm_agent_turn(session, approved=False)]

    events = asyncio.run(_consume())
    assert any(e["event"] == "rejected" for e in events)

    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    row = conn.execute(
        "SELECT status, sql, question FROM agent_queries ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row[0] == "rejected"
    assert row[1] == "SELECT bad()"
    assert row[2] == "上月销售额"


def test_stats_count_rejected():
    insert_agent_query(
        timestamp="2026-08-01T10:00:00+00:00", user="admin",
        question="q", sql="SELECT 1", status="rejected", duration_ms=5.0,
    )
    insert_agent_query(
        timestamp="2026-08-01T10:01:00+00:00", user="admin",
        question="q2", sql="SELECT 2", status="success", duration_ms=5.0,
    )
    from gateway.database import agent_query_stats
    stats = agent_query_stats({})
    assert stats["rejected"] == 1
    assert stats["success"] == 1


# ---------------------------------------------------------------------------
# Correction API
# ---------------------------------------------------------------------------

def test_correction_requires_auth(client):
    resp = client.post("/api/agent/queries/1/correction", json={})
    assert resp.status_code == 401


def test_correction_creates_qa_pair_from_recorded_query(client):
    insert_agent_query(
        timestamp="2026-08-01T10:00:00+00:00", user="admin",
        question="统计上周每天的请求数", sql="SELECT WRONG()", status="rejected",
        duration_ms=5.0,
    )
    qid = query_agent_queries({"page": 1, "page_size": 1})["items"][0]["id"]

    resp = client.post(f"/api/agent/queries/{qid}/correction", headers=AUTH, json={
        "sql_template": "SELECT substr(timestamp,1,10) d, COUNT(*) c "
                        "FROM audit_logs GROUP BY d",
    })
    assert resp.status_code == 200, resp.text
    pair = resp.json()
    assert pair["question"] == "统计上周每天的请求数"
    assert pair["sql_template"].startswith("SELECT substr")
    assert "correction" in pair["tags"]

    stored = _agent_qa_questions()
    assert len(stored) == 1
    assert json.loads(stored[0][2]) == json.loads(pair["tags"])


def test_correction_without_sql_template_uses_recorded_sql(client):
    """Omitting sql_template promotes the SQL that was actually run — the
    admin may have approved a corrected variant in a later turn."""
    insert_agent_query(
        timestamp="2026-08-01T10:00:00+00:00", user="admin",
        question="今天多少请求", sql="SELECT COUNT(*) FROM audit_logs",
        status="success", duration_ms=5.0,
    )
    qid = query_agent_queries({"page": 1, "page_size": 1})["items"][0]["id"]
    resp = client.post(f"/api/agent/queries/{qid}/correction", headers=AUTH, json={})
    assert resp.status_code == 200
    assert resp.json()["sql_template"] == "SELECT COUNT(*) FROM audit_logs"


def test_correction_rejected_record_requires_explicit_sql(client):
    """A rejected record's SQL is known-wrong — promoting it silently would
    poison the knowledge base, so sql_template must be provided explicitly."""
    insert_agent_query(
        timestamp="2026-08-01T10:00:00+00:00", user="admin",
        question="上月销售额", sql="SELECT totally_wrong()", status="rejected",
        duration_ms=5.0,
    )
    qid = query_agent_queries({"page": 1, "page_size": 1})["items"][0]["id"]
    resp = client.post(f"/api/agent/queries/{qid}/correction", headers=AUTH, json={})
    assert resp.status_code == 400
    assert "sql_template" in resp.json()["detail"]


def test_correction_is_idempotent_upsert(client):
    insert_agent_query(
        timestamp="2026-08-01T10:00:00+00:00", user="admin",
        question="重复问题", sql="SELECT 1", status="rejected", duration_ms=1.0,
    )
    qid = query_agent_queries({"page": 1, "page_size": 1})["items"][0]["id"]
    body = {"sql_template": "SELECT 2"}
    first = client.post(f"/api/agent/queries/{qid}/correction", headers=AUTH, json=body)
    second = client.post(f"/api/agent/queries/{qid}/correction", headers=AUTH, json=body)
    assert first.json()["id"] == second.json()["id"]
    assert len(_agent_qa_questions()) == 1


def test_correction_404_for_unknown_query(client):
    resp = client.post("/api/agent/queries/9999/correction", headers=AUTH, json={
        "sql_template": "SELECT 1",
    })
    assert resp.status_code == 404


def test_correction_400_when_no_sql_available(client):
    """A turn that never produced SQL (chitchat / parse error) cannot be
    converted — there is no SQL to learn from."""
    insert_agent_query(
        timestamp="2026-08-01T10:00:00+00:00", user="admin",
        question="你好", sql=None, status="error", duration_ms=1.0,
    )
    qid = query_agent_queries({"page": 1, "page_size": 1})["items"][0]["id"]
    resp = client.post(f"/api/agent/queries/{qid}/correction", headers=AUTH, json={})
    assert resp.status_code == 400


def test_corrected_pair_is_injected_into_prompt(client):
    """End-to-end: after a correction, the agent system prompt carries the
    fixed SQL as a few-shot example for a similar question."""
    from gateway.qa import render_qa_prompt
    insert_agent_query(
        timestamp="2026-08-01T10:00:00+00:00", user="admin",
        question="统计上周每天的请求数", sql="SELECT WRONG()", status="rejected",
        duration_ms=5.0,
    )
    qid = query_agent_queries({"page": 1, "page_size": 1})["items"][0]["id"]
    client.post(f"/api/agent/queries/{qid}/correction", headers=AUTH, json={
        "sql_template": "SELECT substr(timestamp,1,10) AS d, COUNT(*) AS c "
                        "FROM audit_logs GROUP BY d",
    })
    prompt = render_qa_prompt("上周每天的请求数是多少")
    assert "substr(timestamp,1,10)" in prompt
