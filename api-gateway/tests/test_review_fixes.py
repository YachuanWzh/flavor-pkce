"""Fixes for code-review Important issues #1/#2/#3."""

import asyncio
import json
import os
import tempfile
from unittest.mock import patch

import pytest

import gateway.config as config
from gateway.database import init_audit_db, insert_log
from gateway.agent_loop import confirm_agent_turn, run_agent_turn
from gateway.compression import summarize_via_llm
from gateway.session import SessionStore


@pytest.fixture(autouse=True)
def fixed_upstream(monkeypatch):
    monkeypatch.setattr(config, "UPSTREAM_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "UPSTREAM_AUTH_TYPE", "bearer")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "")
    monkeypatch.setattr(config, "UPSTREAM_MODEL", "default")


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="review_fix_test_")
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
    yield
    config.AUDIT_DB_PATH = old
    if os.path.exists(tmp):
        os.remove(tmp)


# ---- Important #1: deltas stream live, not buffered -----------------------


def test_deltas_stream_live_not_buffered():
    """run_agent_turn must yield each delta as the LLM produces it."""
    store = SessionStore(ttl_seconds=600)
    session = store.create()
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    full_text = json.dumps({"intent": "query_data", "sql": sql})

    async def slow_llm(messages, routing=None):
        for i, chunk in enumerate((full_text[:5], full_text[5:10], full_text[10:])):
            await asyncio.sleep(0.01 * (i + 1))
            yield chunk

    observed = []

    async def drive():
        agen = run_agent_turn("how many?", session=session, call_llm=slow_llm)
        while True:
            event = await agen.__anext__()
            observed.append((asyncio.get_event_loop().time(), event))
            if event["event"] == "done":
                break

    asyncio.run(drive())
    deltas = [(t, e) for t, e in observed if e["event"] == "delta"]
    assert len(deltas) == 3
    # Deltas arrive in order with increasing wall-clock time (streamed live,
    # not emitted together at the end).
    times = [t for t, _ in deltas]
    assert times == sorted(times)
    assert times[-1] > times[0] + 0.01


# ---- Important #2: strategy-B summary works on Anthropic upstreams --------


def test_summarize_via_llm_anthropic(monkeypatch):
    routing = {
        "upstream_url": "https://api.deepseek.com/anthropic",
        "upstream_api_key": "sk-ant",
        "upstream_auth_type": "x-api-key",
        "default_model": "claude-3",
        "api_type": "anthropic",
    }
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"content": [{"type": "text", "text": "用户需求: X"}]}

    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _Resp()

    with patch("gateway.compression.httpx.AsyncClient.post", new=fake_post):
        summary = asyncio.run(summarize_via_llm(routing, "history text"))

    assert summary == "用户需求: X"
    assert captured["url"] == "https://api.deepseek.com/anthropic/v1/messages"
    assert captured["headers"].get("x-api-key") == "sk-ant"
    assert captured["json"]["system"]  # the slot prompt is present
    assert "history text" in captured["json"]["messages"][0]["content"]


# ---- Important #3: concurrent turns on one session are serialized ---------


def test_session_state_has_lock():
    from gateway.session import SessionState
    state = SessionState("sid")
    assert hasattr(state, "lock")


def test_concurrent_confirms_single_execution(setup_db):
    """Two concurrent approve requests must not both execute the SQL."""
    store = SessionStore(ttl_seconds=600)
    session = store.create()
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    session.pending_sql = sql
    session.pending_question = "how many?"
    session.pending_attempt = 1

    executions = []

    def counting_execute(sql_arg, **kwargs):
        executions.append(sql_arg)
        return {"columns": ["n"], "rows": [{"n": 1}], "truncated": False}

    async def drive():
        first, second = await asyncio.gather(
            _drain(confirm_agent_turn(session, True, execute_fn=counting_execute)),
            _drain(confirm_agent_turn(session, True, execute_fn=counting_execute)),
        )
        return first, second

    first, second = asyncio.run(drive())
    # Exactly one execution; the other request reports no pending SQL.
    assert len(executions) == 1
    result_events = [e for e in first + second if e["event"] == "result"]
    error_events = [e for e in first + second if e["event"] == "error"]
    assert len(result_events) == 1
    assert len(error_events) == 1


async def _drain(agen):
    return [event async for event in agen]
