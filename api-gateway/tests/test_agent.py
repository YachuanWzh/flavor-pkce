"""NL-to-SQL data agent tests (mock upstream LLM)."""

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest

import gateway.config as config
from gateway.agent import ask_agent, _extract_sql_from_response
from gateway.database import init_audit_db, insert_log


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="agent_test_")
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


def test_extract_sql_from_code_fence():
    text = '```sql\nSELECT "user", COUNT(*) AS n FROM audit_logs GROUP BY "user"\n```'
    assert _extract_sql_from_response(text) == 'SELECT "user", COUNT(*) AS n FROM audit_logs GROUP BY "user"'


def test_extract_sql_from_plain():
    assert _extract_sql_from_response("SELECT COUNT(*) AS n FROM audit_logs") == "SELECT COUNT(*) AS n FROM audit_logs"


def test_extract_sql_none():
    assert _extract_sql_from_response("Sorry, I cannot answer that.") is None


def test_extract_sql_none_content():
    # Regression: an upstream model returning null content must not 500.
    assert _extract_sql_from_response(None) is None


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict):
        self.status_code = status_code
        self._json_body = json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError(
                "Upstream error", request=None, response=self,
            )

    def json(self):
        return self._json_body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_post(fake: _FakeResponse):
    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        return fake
    return fake_post


def test_ask_agent_returns_query_result():
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": '```sql\nSELECT "user", COUNT(*) AS n FROM audit_logs GROUP BY "user"\n```'}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent("How many requests per user?"))

    assert result["sql"].startswith("SELECT")
    assert result["columns"] == ["user", "n"]
    assert result["rows"] == [{"user": "alice", "n": 1}]
    assert result["truncated"] is False


def test_ask_agent_empty_question_raises():
    with pytest.raises(ValueError, match="Question is empty"):
        asyncio.run(ask_agent("   "))


def test_ask_agent_no_sql_raises():
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "I cannot answer that."}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        with pytest.raises(ValueError, match="Could not extract SQL"):
            asyncio.run(ask_agent("anything"))


def test_ask_agent_upstream_error_raises():
    fake = _FakeResponse(502, {"error": "boom"})
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        with pytest.raises(Exception):
            asyncio.run(ask_agent("anything"))
