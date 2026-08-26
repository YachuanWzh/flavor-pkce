"""NL-to-SQL data agent tests (mock upstream LLM)."""

import asyncio
import os
import tempfile
from unittest.mock import patch

import pytest

import gateway.config as config
from gateway.agent import ask_agent, stream_agent, _extract_sql_from_response
from gateway.database import init_audit_db, insert_log


@pytest.fixture(autouse=True)
def fixed_upstream(monkeypatch):
    """Pin upstream config so tests do not depend on api-gateway/.env."""
    monkeypatch.setattr(config, "UPSTREAM_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "UPSTREAM_AUTH_TYPE", "bearer")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "")
    monkeypatch.setattr(config, "UPSTREAM_MODEL", "default")


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
        self.calls: list[dict] = []

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
        fake.calls.append({"url": url, "headers": headers or {}, "json": json})
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


def test_ask_agent_anthropic_upstream_uses_messages_api(monkeypatch):
    """Regression: DeepSeek /anthropic base + x-api-key must use the
    Anthropic Messages API (not OpenAI /chat/completions with Bearer)."""
    monkeypatch.setattr(config, "UPSTREAM_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setattr(config, "UPSTREAM_AUTH_TYPE", "x-api-key")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "UPSTREAM_MODEL", "deepseek-chat")
    fake = _FakeResponse(200, {
        "content": [{"type": "text", "text": '```sql\nSELECT "user", COUNT(*) AS n FROM audit_logs GROUP BY "user"\n```'}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent("How many requests per user?"))

    call = fake.calls[-1]
    assert call["url"] == "https://api.deepseek.com/anthropic/v1/messages"
    assert call["headers"].get("x-api-key") == "sk-test"
    assert "Authorization" not in call["headers"]
    assert call["json"]["model"] == "deepseek-chat"
    assert "system" in call["json"] and call["json"]["system"]
    assert call["json"]["messages"] == [{"role": "user", "content": "How many requests per user?"}]
    assert result["columns"] == ["user", "n"]
    assert result["rows"] == [{"user": "alice", "n": 1}]


def test_ask_agent_openai_upstream_bearer_auth(monkeypatch):
    monkeypatch.setattr(config, "UPSTREAM_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "UPSTREAM_AUTH_TYPE", "bearer")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "UPSTREAM_MODEL", "gpt-4o-mini")
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT COUNT(*) AS n FROM audit_logs"}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent("how many?"))

    call = fake.calls[-1]
    assert call["url"] == "https://api.openai.com/v1/chat/completions"
    assert call["headers"].get("Authorization") == "Bearer sk-test"
    assert call["json"]["model"] == "gpt-4o-mini"
    assert result["columns"] == ["n"]


def test_ask_agent_openai_upstream_x_api_key_auth(monkeypatch):
    monkeypatch.setattr(config, "UPSTREAM_URL", "https://api.deepseek.com")
    monkeypatch.setattr(config, "UPSTREAM_AUTH_TYPE", "x-api-key")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "UPSTREAM_MODEL", "deepseek-chat")
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT COUNT(*) AS n FROM audit_logs"}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent("how many?"))

    call = fake.calls[-1]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert call["headers"].get("x-api-key") == "sk-test"
    assert "Authorization" not in call["headers"]
    assert result["columns"] == ["n"]


def test_ask_agent_uses_user_routing():
    """Agent must use the signed-in user's LLM config (routing dict), not the
    gateway-wide UPSTREAM_* env config."""
    routing = {
        "upstream_url": "https://api.deepseek.com/anthropic",
        "upstream_api_key": "user-secret-key",
        "upstream_auth_type": "x-api-key",
        "default_model": "user-model",
        "api_type": "anthropic",
    }
    fake = _FakeResponse(200, {
        "content": [{"type": "text", "text": "SELECT COUNT(*) AS n FROM audit_logs"}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent("how many?", routing=routing))

    call = fake.calls[-1]
    assert call["url"] == "https://api.deepseek.com/anthropic/v1/messages"
    assert call["headers"].get("x-api-key") == "user-secret-key"
    assert "Authorization" not in call["headers"]
    assert call["json"]["model"] == "user-model"
    assert result["columns"] == ["n"]


def test_ask_agent_user_routing_openai_profile():
    """User routing with an OpenAI api_type must hit /chat/completions."""
    routing = {
        "upstream_url": "https://gateway.example.com/v1",
        "upstream_api_key": "user-openai-key",
        "upstream_auth_type": "bearer",
        "default_model": "user-gpt",
        "api_type": "openai",
    }
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT COUNT(*) AS n FROM audit_logs"}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent("how many?", routing=routing))

    call = fake.calls[-1]
    assert call["url"] == "https://gateway.example.com/v1/chat/completions"
    assert call["headers"].get("Authorization") == "Bearer user-openai-key"
    assert call["json"]["model"] == "user-gpt"
    assert result["columns"] == ["n"]


def test_stream_agent_emits_deltas_sql_and_result(monkeypatch):
    async def fake_stream(question, routing=None):
        assert question == "how many?"
        yield "SELECT COUNT(*) "
        yield "AS n FROM audit_logs"

    monkeypatch.setattr("gateway.agent._stream_upstream", fake_stream)

    async def collect():
        return [item async for item in stream_agent("how many?")]

    events = asyncio.run(collect())
    assert [item["event"] for item in events] == [
        "status", "delta", "delta", "sql", "status", "result", "done",
    ]
    assert events[1]["data"]["text"] == "SELECT COUNT(*) "
    assert events[3]["data"]["sql"] == "SELECT COUNT(*) AS n FROM audit_logs"
    assert events[5]["data"]["rows"] == [{"n": 1}]
