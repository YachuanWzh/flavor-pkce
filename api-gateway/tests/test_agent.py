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


def test_agent_records_audit_entry():
    """P0-1: every agent ask must be recorded in the agent_queries table."""
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT COUNT(*) AS n FROM audit_logs"}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent("how many?", user="alice"))

    from gateway.database import query_agent_queries
    items = query_agent_queries({"page": 1, "page_size": 10})["items"]
    assert len(items) == 1
    row = items[0]
    assert row["user"] == "alice"
    assert row["question"] == "how many?"
    assert row["sql"] == "SELECT COUNT(*) AS n FROM audit_logs"
    assert row["status"] == "success"
    assert row["rows_returned"] == 1
    assert row["error"] is None
    assert result["rows"] == [{"n": 1}]


def test_agent_records_failed_query():
    """P0-1: failed executions are recorded with the error message."""
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT nope FROM audit_logs"}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        with pytest.raises(Exception):
            asyncio.run(ask_agent("bad query", user="bob"))

    from gateway.database import query_agent_queries
    items = query_agent_queries({"page": 1, "page_size": 10})["items"]
    assert len(items) == 1
    row = items[0]
    assert row["user"] == "bob"
    assert row["status"] == "error"
    assert row["sql"] == "SELECT nope FROM audit_logs"
    assert row["error"]


def test_system_prompt_includes_today_utc_date():
    """P0-2: the system prompt must carry today's UTC date so relative
    ranges like 'last 7 days' resolve deterministically."""
    from datetime import datetime, timezone
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT 1 AS n"}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        asyncio.run(ask_agent("how many?"))

    today = datetime.now(timezone.utc).date().isoformat()
    messages = fake.calls[-1]["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert f"Today's date is {today}" in messages[0]["content"]
    assert messages[0]["content"].startswith("You are a read-only analytics assistant")


def test_system_prompt_includes_today_date_anthropic(monkeypatch):
    """P0-2: the Anthropic branch also carries the UTC date."""
    from datetime import datetime, timezone
    monkeypatch.setattr(config, "UPSTREAM_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setattr(config, "UPSTREAM_AUTH_TYPE", "x-api-key")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-test")
    fake = _FakeResponse(200, {
        "content": [{"type": "text", "text": "SELECT 1 AS n"}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        asyncio.run(ask_agent("how many?"))

    today = datetime.now(timezone.utc).date().isoformat()
    system = fake.calls[-1]["json"]["system"]
    assert f"Today's date is {today}" in system


def test_agent_mode_denies_body_columns_by_default():
    """P0-1: the agent itself must run with sensitive columns blocked."""
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT request_body FROM audit_logs LIMIT 1"}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        with pytest.raises(Exception):
            asyncio.run(ask_agent("show request bodies", user="alice"))


def test_ask_agent_includes_history_context():
    """P2-6: previous question/SQL turns are injected as context so follow-up
    questions like 'group by user?' resolve against the earlier query."""
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT COUNT(*) AS n FROM audit_logs"}}]
    })
    history = [
        {"question": "how many requests?", "sql": "SELECT COUNT(*) FROM audit_logs"},
    ]
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent(
            "group by user?", user="alice", history=history,
        ))

    call = fake.calls[-1]
    content = call["json"]["messages"][-1]["content"]
    assert "how many requests?" in content
    assert "SELECT COUNT(*) FROM audit_logs" in content
    assert "group by user?" in content
    assert result["columns"] == ["n"]


def test_ask_agent_empty_history_is_fine():
    fake = _FakeResponse(200, {
        "choices": [{"message": {"content": "SELECT COUNT(*) AS n FROM audit_logs"}}]
    })
    with patch("gateway.agent.httpx.AsyncClient.post", new=_fake_post(fake)):
        result = asyncio.run(ask_agent("how many?", user="alice", history=[]))
    assert result["rows"] == [{"n": 1}]


def test_ask_agent_retries_on_sql_error():
    """P1-3: a failing generated SQL is fed back to the model; the corrected
    second attempt succeeds and only the final result is returned."""
    calls: list[dict] = []
    responses = [
        {"choices": [{"message": {"content": "SELECT * FROM nonexistent_table"}}]},
        {"choices": [{"message": {"content": "SELECT COUNT(*) AS n FROM audit_logs"}}]},
    ]

    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakeResponse(200, responses[len(calls) - 1])

    with patch("gateway.agent.httpx.AsyncClient.post", new=fake_post):
        result = asyncio.run(ask_agent("how many?", user="alice"))

    assert len(calls) == 2
    # The second round-trip carries the failed SQL and the DB error.
    second_content = calls[1]["json"]["messages"][-1]["content"]
    assert "nonexistent_table" in second_content
    assert "not allowed" in second_content.lower()
    assert result["rows"] == [{"n": 1}]

    # Exactly one success record: the retry is internal, not a new interaction.
    from gateway.database import query_agent_queries
    items = query_agent_queries({"page": 1, "page_size": 10})["items"]
    assert len(items) == 1
    assert items[0]["status"] == "success"


def test_ask_agent_gives_up_after_max_retries():
    """P1-3: after 2 correction rounds the original error surfaces."""
    calls: list[str] = []

    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        calls.append(url)
        return _FakeResponse(200, {
            "choices": [{"message": {"content": "SELECT * FROM nonexistent_table"}}]
        })

    with patch("gateway.agent.httpx.AsyncClient.post", new=fake_post):
        with pytest.raises(Exception):
            asyncio.run(ask_agent("how many?", user="alice"))

    # Initial generation + 2 correction rounds.
    assert len(calls) == 3


def test_stream_agent_retries_with_corrected_sql(monkeypatch):
    """P1-3: the streamed agent emits a retrying status, then replaces the
    SQL with the corrected statement and succeeds."""
    sequence = ["SELECT * FROM nonexistent_table", "SELECT COUNT(*) AS n FROM audit_logs"]
    calls = {"n": 0}

    async def fake_stream(question, routing=None, previous_attempts=None, history=None):
        text = sequence[calls["n"]]
        calls["n"] += 1
        yield text[:10]
        yield text[10:]

    monkeypatch.setattr("gateway.agent._stream_upstream", fake_stream)

    async def collect():
        return [item async for item in stream_agent("how many?", user="alice")]

    events = asyncio.run(collect())
    assert [e["event"] for e in events] == [
        "status", "delta", "delta", "sql",   # first generation, fails
        "status",                            # retrying
        "sql", "status", "result", "done",   # corrected SQL succeeds
    ]
    result_event = next(e for e in events if e["event"] == "result")
    assert result_event["data"]["sql"] == "SELECT COUNT(*) AS n FROM audit_logs"
    assert result_event["data"]["rows"] == [{"n": 1}]


def test_stream_agent_emits_deltas_sql_and_result(monkeypatch):
    async def fake_stream(question, routing=None, previous_attempts=None, history=None):
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
