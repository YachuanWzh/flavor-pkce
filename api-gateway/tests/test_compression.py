"""Two-level context compression tests (agent-loop Task 4)."""

import asyncio
import json
from unittest.mock import patch

import gateway.config as config
from gateway.compression import (
    compress_context,
    estimate_tokens,
    summarize_via_llm,
    SUMMARY_SYSTEM_PROMPT,
    RECENT_TURNS_KEPT,
)


def _turn(question, sql, rows=1):
    """One conversation turn: user question, assistant SQL, tool result."""
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": json.dumps({"intent": "query_data", "sql": sql})},
        {"role": "tool", "content": json.dumps({"columns": ["n"], "rows": [{"n": rows}]})},
    ]


def _long_history(turns=10):
    messages = []
    for i in range(turns):
        messages += _turn(f"question number {i}", f"SELECT {i} AS n FROM audit_logs")
    return messages


def test_estimate_tokens_grows_with_length():
    assert estimate_tokens("") == 0
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("x" * 10) >= 1


def test_below_threshold_unchanged():
    messages = _long_history(2)
    result = compress_context(messages, threshold_tokens=10_000_000)
    assert result["messages"] == messages
    assert result["strategy"] is None


def test_strategy_a_keeps_last_5_turns_and_compresses_older():
    """a. keep the last 5 turns verbatim, compress older tool-call records."""
    from gateway.compression import estimate_messages_tokens

    messages = []
    for i in range(10):
        long_q = f"question number {i} " + "x" * 800
        messages += _turn(long_q, f"SELECT {i} AS n FROM audit_logs")
    original_tokens = estimate_messages_tokens(messages)
    threshold = int(original_tokens * 0.85)

    result = compress_context(messages, threshold_tokens=threshold)
    out = result["messages"]
    assert result["strategy"] == "turn_compress"
    assert estimate_messages_tokens(out) <= threshold

    # The last 5 turns survive verbatim at the tail.
    expected_tail = messages[-RECENT_TURNS_KEPT * 3:]
    assert out[-RECENT_TURNS_KEPT * 3:] == expected_tail

    # Older turns are compressed: each becomes a single compact record
    # (never longer than the original turn it replaces).
    older = out[: -RECENT_TURNS_KEPT * 3]
    assert len(older) < len(messages) - RECENT_TURNS_KEPT * 3
    for item in older:
        assert item["role"] == "compressed"
    assert any("question number 0" in item["content"] for item in older)


def test_strategy_b_uses_llm_summary_when_a_still_over():
    """b. if repeated a-compression still exceeds the threshold, run a
    whole-context LLM summary."""
    messages = _long_history(10)
    calls: list[str] = []

    def fake_summarize(text):
        calls.append(text)
        return (
            "用户需求: 统计审计日志。已执行步骤: 查询了10次。"
            "未执行步骤: 无。用户偏好: 无。"
        )

    result = compress_context(
        messages,
        threshold_tokens=1,
        keep_recent_5_tokens=True,  # keep last 5 turns even after summary
        summarize_fn=fake_summarize,
    )
    assert result["strategy"] == "llm_summary"
    assert len(calls) == 1
    # The older turns were handed to the summarizer.
    assert "question number 0" in calls[0]

    out = result["messages"]
    assert out[0]["role"] == "summary"
    assert "用户需求" in out[0]["content"]
    # Recent turns still present verbatim.
    assert out[-3:] == messages[-3:]


def test_strategy_b_without_keep_recent_replaces_everything():
    messages = _long_history(10)
    result = compress_context(
        messages,
        threshold_tokens=1,
        summarize_fn=lambda text: "full summary",
    )
    assert result["strategy"] == "llm_summary"
    assert result["messages"] == [{"role": "summary", "content": "full summary"}]


def test_summary_system_prompt_mentions_required_slots():
    for keyword in ("用户需求", "已执行步骤", "未执行步骤", "用户偏好"):
        assert keyword in SUMMARY_SYSTEM_PROMPT


def test_summarize_via_llm_parses_response(monkeypatch):
    monkeypatch.setattr(config, "UPSTREAM_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "UPSTREAM_AUTH_TYPE", "bearer")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "sk-test")
    monkeypatch.setattr(config, "UPSTREAM_MODEL", "gpt-mini")

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "需求: X"}}]}

    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        return _Resp()

    with patch("gateway.compression.httpx.AsyncClient.post", new=fake_post):
        summary = asyncio.run(summarize_via_llm(None, "older turns text"))
    assert summary == "需求: X"


def test_summarize_via_llm_falls_back_to_routing(monkeypatch):
    """Routing dict (user LLM config) is preferred over gateway env."""
    routing = {
        "upstream_url": "https://api.deepseek.com",
        "upstream_api_key": "user-key",
        "upstream_auth_type": "bearer",
        "default_model": "user-model",
        "api_type": "openai",
    }
    captured = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "sum"}}]}

    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    with patch("gateway.compression.httpx.AsyncClient.post", new=fake_post):
        summary = asyncio.run(summarize_via_llm(routing, "text"))
    assert summary == "sum"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer user-key"
