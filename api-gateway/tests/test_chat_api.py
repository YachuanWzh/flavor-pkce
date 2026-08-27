"""API tests for the session-based agent chat endpoints (agent-loop Task 6)."""

import json
import os
import tempfile
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_log


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="chat_api_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    old_token = config.AUDIT_API_TOKEN
    config.AUDIT_DB_PATH = tmp
    config.AUDIT_API_TOKEN = "test-audit-token"
    init_audit_db()
    insert_log(
        timestamp="2026-08-01T10:00:00+00:00",
        user="alice", method="POST", path="/v1/messages",
        status=200, duration_ms=500.0, upstream_ms=450.0, level="INFO",
        prompt_tokens=150, completion_tokens=80, model="claude-sonnet-4-5",
    )
    yield
    config.AUDIT_DB_PATH = old
    config.AUDIT_API_TOKEN = old_token
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


AUTH = {"X-Audit-Token": "test-audit-token"}
SQL = "SELECT COUNT(*) AS n FROM audit_logs"


def _sse_blocks(text):
    """Parse an SSE body into [(event, data_dict)]."""
    events = []
    for block in text.strip().split("\n\n"):
        event = "message"
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


async def _chitchat_stream(message, *, session, routing, user, user_id):
    yield {"event": "session", "data": {"session_id": session.session_id}}
    yield {"event": "status", "data": {"stage": "generating", "message": "..."}}
    yield {"event": "delta", "data": {"text": "hi"}}
    yield {"event": "intent", "data": {"intent": "chitchat", "message": "hi"}}
    yield {"event": "message", "data": {"message": "hi", "intent": "chitchat"}}
    yield {"event": "done", "data": {}}


async def _query_stream(message, *, session, routing, user, user_id):
    yield {"event": "session", "data": {"session_id": session.session_id}}
    yield {"event": "status", "data": {"stage": "generating", "message": "..."}}
    yield {"event": "intent", "data": {"intent": "query_data", "sql": SQL}}
    yield {"event": "confirmation_required", "data": {"sql": SQL, "attempt": 1}}
    yield {"event": "done", "data": {}}


def test_chat_requires_auth(client):
    resp = client.post("/api/agent/chat", json={"message": "hi"})
    assert resp.status_code == 401


def test_chat_empty_message_400(client):
    resp = client.post("/api/agent/chat", headers=AUTH, json={"message": "   "})
    assert resp.status_code == 400


def test_chat_sse_chitchat(client):
    with patch("gateway.main._chat_stream_events", new=_chitchat_stream):
        resp = client.post("/api/agent/chat", headers=AUTH, json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers.get("cache-control") == "no-cache"
    events = _sse_blocks(resp.text)
    names = [name for name, _ in events]
    assert "session" in names and "done" in names
    assert any(name == "delta" and data["text"] == "hi" for name, data in events)


def test_chat_keeps_session_across_requests(client):
    """The server returns a session_id; sending it back reuses the session."""
    seen_sessions = []

    async def spy(message, *, session, routing, user, user_id):
        seen_sessions.append(session)
        async for ev in _chitchat_stream(message, session=session, routing=routing, user=user, user_id=user_id):
            yield ev

    with patch("gateway.main._chat_stream_events", new=spy):
        resp = client.post("/api/agent/chat", headers=AUTH, json={"message": "hi"})
        session_id = next(data["session_id"] for name, data in _sse_blocks(resp.text) if name == "session")
        assert session_id
        resp2 = client.post(
            "/api/agent/chat", headers=AUTH,
            json={"message": "again", "session_id": session_id},
        )
    assert resp2.status_code == 200
    # The same session object was reused for the second turn.
    assert len(seen_sessions) == 2
    assert seen_sessions[0] is seen_sessions[1]
    assert seen_sessions[1].session_id == session_id


def test_chat_unknown_session_starts_fresh(client):
    with patch("gateway.main._chat_stream_events", new=_chitchat_stream):
        resp = client.post(
            "/api/agent/chat", headers=AUTH,
            json={"message": "hi", "session_id": "does-not-exist"},
        )
    assert resp.status_code == 200
    events = _sse_blocks(resp.text)
    new_id = next(data["session_id"] for name, data in events if name == "session")
    assert new_id != "does-not-exist"


def test_confirm_requires_auth(client):
    resp = client.post("/api/agent/chat/confirm", json={"session_id": "x", "approved": True})
    assert resp.status_code == 401


def test_confirm_flow_executes_after_approval(client):
    """Full round trip: chat → confirmation_required → confirm(approve) → result."""
    with patch("gateway.main._chat_stream_events", new=_query_stream):
        resp = client.post("/api/agent/chat", headers=AUTH, json={"message": "how many?"})
    session_id = next(data["session_id"] for name, data in _sse_blocks(resp.text) if name == "session")
    assert any(name == "confirmation_required" for name, _ in _sse_blocks(resp.text))

    async def confirm_stream(session, approved, routing=None, user="-", user_id=None):
        yield {"event": "session", "data": {"session_id": session.session_id}}
        yield {"event": "result", "data": {"sql": SQL, "columns": ["n"], "rows": [{"n": 1}], "truncated": False}}
        yield {"event": "done", "data": {}}

    with patch("gateway.main._confirm_stream_events", new=confirm_stream):
        resp2 = client.post(
            "/api/agent/chat/confirm", headers=AUTH,
            json={"session_id": session_id, "approved": True},
        )
    assert resp2.status_code == 200
    events = _sse_blocks(resp2.text)
    result = next(data for name, data in events if name == "result")
    assert result["rows"] == [{"n": 1}]


def test_confirm_unknown_session_404(client):
    resp = client.post(
        "/api/agent/chat/confirm", headers=AUTH,
        json={"session_id": "nope", "approved": True},
    )
    assert resp.status_code == 404


def test_confirm_reject_endpoint(client):
    seen = {}

    async def spy_chat(message, *, session, routing, user, user_id):
        async for ev in _query_stream(message, session=session, routing=routing, user=user, user_id=user_id):
            yield ev

    with patch("gateway.main._chat_stream_events", new=spy_chat):
        resp = client.post("/api/agent/chat", headers=AUTH, json={"message": "how many?"})
    session_id = next(data["session_id"] for name, data in _sse_blocks(resp.text) if name == "session")

    async def reject_stream(session, approved, routing=None, user="-", user_id=None):
        seen["approved"] = approved
        yield {"event": "rejected", "data": {}}
        yield {"event": "done", "data": {}}

    with patch("gateway.main._confirm_stream_events", new=reject_stream):
        resp2 = client.post(
            "/api/agent/chat/confirm", headers=AUTH,
            json={"session_id": session_id, "approved": False},
        )
    assert resp2.status_code == 200
    assert seen["approved"] is False
    assert any(name == "rejected" for name, _ in _sse_blocks(resp2.text))


def test_chat_stream_error_event_surfaces(client):
    async def failing_stream(message, *, session, routing, user, user_id):
        yield {"event": "session", "data": {"session_id": session.session_id}}
        yield {"event": "error", "data": {"message": "boom"}}
        yield {"event": "done", "data": {}}

    with patch("gateway.main._chat_stream_events", new=failing_stream):
        resp = client.post("/api/agent/chat", headers=AUTH, json={"message": "hi"})
    assert resp.status_code == 200
    assert any(name == "error" for name, _ in _sse_blocks(resp.text))
