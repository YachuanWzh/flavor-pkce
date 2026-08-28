"""Agent loop tests: intent routing, SQL confirmation, reflection retries,
compression integration (agent-loop Task 5)."""

import asyncio
import json
import os
import tempfile

import pytest

import gateway.config as config
from gateway.database import init_audit_db, insert_log
from gateway.agent_loop import (
    MAX_REFLECT_RETRIES,
    confirm_agent_turn,
    run_agent_turn,
)
from gateway.session import SessionStore


@pytest.fixture(autouse=True)
def fixed_upstream(monkeypatch):
    monkeypatch.setattr(config, "UPSTREAM_URL", "https://api.openai.com/v1")
    monkeypatch.setattr(config, "UPSTREAM_AUTH_TYPE", "bearer")
    monkeypatch.setattr(config, "UPSTREAM_API_KEY", "")
    monkeypatch.setattr(config, "UPSTREAM_MODEL", "default")


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="agent_loop_test_")
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


@pytest.fixture
def store():
    return SessionStore(ttl_seconds=600)


def make_llm(responses):
    """Return (call_llm, calls) where call_llm yields responses in order."""
    calls = {"n": 0, "messages_seen": []}

    async def call_llm(messages, routing=None):
        calls["messages_seen"].append([dict(m) for m in messages])
        text = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        for chunk in (text[:10], text[10:]) if len(text) > 10 else (text,):
            yield chunk

    return call_llm, calls


def collect(agen):
    return asyncio.run(_drain(agen))


async def _drain(agen):
    return [event async for event in agen]


def events_of(events, name):
    return [e for e in events if e["event"] == name]


def intent_sql(sql, attempt=1):
    return json.dumps({"intent": "query_data", "sql": sql})


# ---- chitchat -------------------------------------------------------------


def test_chitchat_replies_without_confirmation(store):
    session = store.create()
    llm, calls = make_llm([json.dumps({"intent": "chitchat", "message": "你好!"})])

    events = collect(run_agent_turn(
        "hello", session=session, call_llm=llm,
    ))
    names = [e["event"] for e in events]

    assert names[0] == "session"
    assert "confirmation_required" not in names
    assert "result" not in names
    intent_event = events_of(events, "intent")[0]
    assert intent_event["data"]["intent"] == "chitchat"
    message_event = events_of(events, "message")[0]
    assert message_event["data"]["message"] == "你好!"
    assert names[-1] == "done"
    assert session.pending_sql is None
    # The turn is recorded in session messages.
    assert session.messages[-2]["role"] == "user"
    assert session.messages[-1]["role"] == "assistant"


# ---- query_data flow -------------------------------------------------------


def test_query_data_emits_confirmation_required_and_stops(store):
    session = store.create()
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    llm, calls = make_llm([intent_sql(sql)])

    events = collect(run_agent_turn(
        "how many?", session=session, call_llm=llm,
    ))
    names = [e["event"] for e in events]

    assert "delta" in names
    confirm = events_of(events, "confirmation_required")[0]
    assert confirm["data"]["sql"] == sql
    assert confirm["data"]["attempt"] == 1
    assert names[-1] == "done"
    # SQL is NOT executed before confirmation.
    assert events_of(events, "result") == []
    assert session.pending_sql == sql


def test_confirm_approved_executes_and_returns_result(store):
    session = store.create()
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    llm, calls = make_llm([intent_sql(sql)])
    collect(run_agent_turn("how many?", session=session, call_llm=llm))

    events = collect(confirm_agent_turn(
        session, approved=True, call_llm=llm,
    ))
    result = events_of(events, "result")[0]
    assert result["data"]["sql"] == sql
    assert result["data"]["rows"] == [{"n": 1}]
    assert [e["event"] for e in events][-1] == "done"
    # Pending state cleared; tool result recorded for context.
    assert session.pending_sql is None
    assert session.messages[-1]["role"] == "tool"
    assert '"rows"' in session.messages[-1]["content"]


def test_confirm_rejected_clears_pending(store):
    session = store.create()
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    llm, _ = make_llm([intent_sql(sql)])
    collect(run_agent_turn("how many?", session=session, call_llm=llm))

    events = collect(confirm_agent_turn(session, approved=False, call_llm=llm))
    names = [e["event"] for e in events]
    assert "rejected" in names
    assert "result" not in names
    assert session.pending_sql is None


def test_confirm_without_pending_sql_errors(store):
    session = store.create()
    llm, _ = make_llm([])
    events = collect(confirm_agent_turn(session, approved=True, call_llm=llm))
    assert events_of(events, "error")


# ---- chart suggestions -----------------------------------------------------


def test_chart_flows_from_intent_to_result(store):
    session = store.create()
    sql = "SELECT date, requests FROM v_audit_daily"
    chart = {"type": "line", "x": "date", "series": "requests"}
    llm, _ = make_llm([json.dumps({"intent": "query_data", "sql": sql, "chart": chart})])

    run_events = collect(run_agent_turn("show trend", session=session, call_llm=llm))
    conf = events_of(run_events, "confirmation_required")[0]
    assert conf["data"]["sql"] == sql

    confirm_events = collect(confirm_agent_turn(session, True, call_llm=llm))
    result = events_of(confirm_events, "result")[0]
    assert result["data"]["chart"] == chart


def test_chart_cleared_on_rejection(store):
    session = store.create()
    sql = "SELECT date, requests FROM v_audit_daily"
    chart = {"type": "line", "x": "date", "series": "requests"}
    llm, _ = make_llm([json.dumps({"intent": "query_data", "sql": sql, "chart": chart})])

    collect(run_agent_turn("show trend", session=session, call_llm=llm))
    collect(confirm_agent_turn(session, False, call_llm=llm))
    assert session.pending_chart is None


def test_auto_chart_inferred_when_llm_omits_chart(store):
    session = store.create()
    # LLM returns NO chart field; result has a date column + numeric column.
    sql = "SELECT substr(timestamp, 1, 10) AS date, COUNT(*) AS requests FROM audit_logs GROUP BY date"
    llm, _ = make_llm([intent_sql(sql)])
    collect(run_agent_turn("daily trend", session=session, call_llm=llm))

    events = collect(confirm_agent_turn(session, True, call_llm=llm))
    result = events_of(events, "result")[0]
    assert result["data"]["chart"] == {"type": "line", "x": "date", "series": "requests"}


def test_no_auto_chart_without_date_column(store):
    session = store.create()
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    llm, _ = make_llm([intent_sql(sql)])
    collect(run_agent_turn("how many", session=session, call_llm=llm))

    events = collect(confirm_agent_turn(session, True, call_llm=llm))
    result = events_of(events, "result")[0]
    assert "chart" not in result["data"]


# ---- metric-term prompt injection ------------------------------------------


def test_system_prompt_includes_metric_terms(monkeypatch):
    from gateway import terms
    from gateway.agent_loop import _build_system_prompt as loop_prompt

    def fake_terms(*_a, **_k):
        return [
            {"term": "cache_hit_ratio", "definition": "cache_read / total", "synonyms": "[\"命中率\"]", "enabled": True}
        ]

    monkeypatch.setattr(terms, "list_metric_terms", fake_terms)
    text = loop_prompt()
    assert "cache_hit_ratio" in text
    assert "命中率" in text


def test_system_prompt_instructs_chart_output():
    from gateway.agent_loop import _SYSTEM_PROMPT

    assert '"chart"' in _SYSTEM_PROMPT
    # The prompt must tell the model which chart types it may suggest.
    assert "bar" in _SYSTEM_PROMPT and "line" in _SYSTEM_PROMPT and "pie" in _SYSTEM_PROMPT


def test_system_prompts_define_total_tokens_with_cache():
    """NL2SQL must not invent the old prompt+completion-only "total tokens"
    definition: agent traffic is dominated by cache reads, so totals have
    to include both cache columns. Both agent prompts and the schema
    descriptions shown to the model must state the canonical formula AND
    the per-column COALESCE(SUM(...)) aggregation that matches the
    dashboard (per-row SUM(a+b+c+d) silently drops NULL rows)."""
    from gateway.agent import _SYSTEM_PROMPT as ask_prompt
    from gateway.agent_loop import _SYSTEM_PROMPT as loop_prompt
    from gateway.query import SCHEMA_DESCRIPTIONS

    formula = "prompt_tokens + completion_tokens + cache_read_tokens + cache_creation_tokens"
    per_column = "COALESCE(SUM(prompt_tokens), 0)"
    for prompt in (ask_prompt, loop_prompt):
        assert formula in prompt
        assert per_column in prompt
    for description in SCHEMA_DESCRIPTIONS.values():
        assert formula in description
        assert per_column in description


# ---- reflection retries ----------------------------------------------------


def test_execution_error_reflects_and_reconfirms(store):
    """Requirement 2: feed the execution error back to the loop; the new
    SQL must be confirmed again before executing."""
    session = store.create()
    bad_sql = 'SELECT COUNT(*) AS n FROM audit_logs WHERE "user" = nope'
    good_sql = "SELECT COUNT(*) AS n FROM audit_logs"
    llm, calls = make_llm([intent_sql(bad_sql), intent_sql(good_sql)])

    results = {"n": 0}

    def flaky_execute(sql, **kwargs):
        results["n"] += 1
        if "nope" in sql:
            raise ValueError("no such column: nope")
        return {"columns": ["n"], "rows": [{"n": 1}], "truncated": False}

    collect(run_agent_turn("how many?", session=session, call_llm=llm))
    assert session.pending_sql == bad_sql

    events = collect(confirm_agent_turn(
        session, approved=True, call_llm=llm, execute_fn=flaky_execute,
    ))
    names = [e["event"] for e in events]

    # The failed execution triggered reflection: the model was called again,
    # and the corrected SQL now waits for a new confirmation.
    assert "retrying" in names
    confirm = events_of(events, "confirmation_required")[0]
    assert confirm["data"]["sql"] == good_sql
    assert confirm["data"]["attempt"] == 2
    assert "result" not in names
    assert session.pending_sql == good_sql

    # The error was fed back to the model for reflection.
    reflected = calls["messages_seen"][-1]
    flat = " ".join(m["content"] for m in reflected)
    assert "no such column: nope" in flat

    # Second confirmation executes the corrected SQL.
    events = collect(confirm_agent_turn(
        session, approved=True, call_llm=llm, execute_fn=flaky_execute,
    ))
    result = events_of(events, "result")[0]
    assert result["data"]["rows"] == [{"n": 1}]


def test_retry_cap_three_then_error(store):
    """Requirement 2: at most 3 reflection retries after execution errors."""
    session = store.create()
    sql = 'SELECT COUNT(*) AS n FROM audit_logs WHERE "user" = nope'
    llm, calls = make_llm([intent_sql(sql)])

    def always_fail(sql_arg, **kwargs):
        raise ValueError("no such column: nope")

    collect(run_agent_turn("how many?", session=session, call_llm=llm))

    attempts_seen = []
    while True:
        events = collect(confirm_agent_turn(
            session, approved=True, call_llm=llm, execute_fn=always_fail,
        ))
        confirm = events_of(events, "confirmation_required")
        if confirm:
            attempts_seen.append(confirm[0]["data"]["attempt"])
        else:
            break
    # Confirmations for reflection attempts 2, 3, 4 — then the cap stops it.
    assert attempts_seen == [2, 3, 4]
    assert events_of(events, "error")
    assert session.pending_sql is None


def test_dangerous_sql_blocked_then_reflection_recovers(store):
    """Requirement 9: dangerous SQL is intercepted and never executed; the
    block reason is fed back so the model can retry."""
    session = store.create()
    responses = [
        json.dumps({"intent": "query_data", "sql": "DROP TABLE audit_logs"}),
        intent_sql("SELECT COUNT(*) AS n FROM audit_logs"),
    ]
    llm, calls = make_llm(responses)

    executed = []

    def spy_execute(sql, **kwargs):
        executed.append(sql)
        return {"columns": ["n"], "rows": [{"n": 1}], "truncated": False}

    events = collect(run_agent_turn(
        "delete everything", session=session, call_llm=llm,
        execute_fn=spy_execute,
    ))
    names = [e["event"] for e in events]

    blocked = events_of(events, "blocked")[0]
    assert "DROP" in blocked["data"]["reason"].upper()
    assert blocked["data"]["sql"] == "DROP TABLE audit_logs"
    # Recovery: after reflection the safe SQL awaits confirmation.
    confirm = events_of(events, "confirmation_required")[0]
    assert confirm["data"]["sql"] == "SELECT COUNT(*) AS n FROM audit_logs"
    assert executed == []  # nothing ever ran
    # The block reason reached the model.
    flat = " ".join(m["content"] for m in calls["messages_seen"][-1])
    assert "disallowed" in flat.lower()


def test_dangerous_sql_exhausting_retries_ends_blocked(store):
    session = store.create()
    responses = [
        json.dumps({"intent": "query_data", "sql": "DELETE FROM audit_logs"}),
    ]
    llm, _ = make_llm(responses)

    executed = []

    def spy_execute(sql, **kwargs):
        executed.append(sql)
        return {"columns": [], "rows": [], "truncated": False}

    events = collect(run_agent_turn(
        "wipe it", session=session, call_llm=llm, execute_fn=spy_execute,
    ))
    assert events_of(events, "blocked")
    assert events_of(events, "error")
    assert [e["event"] for e in events][-1] == "done"
    assert executed == []
    assert session.pending_sql is None


def test_invalid_intent_json_reflection(store):
    """Requirement 8: malformed intent JSON is fed back for correction."""
    session = store.create()
    responses = [
        "Sure, the answer is SELECT 1!",
        intent_sql("SELECT COUNT(*) AS n FROM audit_logs"),
    ]
    llm, calls = make_llm(responses)

    events = collect(run_agent_turn(
        "how many?", session=session, call_llm=llm,
    ))
    names = [e["event"] for e in events]
    assert "retrying" in names
    confirm = events_of(events, "confirmation_required")[0]
    assert confirm["data"]["sql"] == "SELECT COUNT(*) AS n FROM audit_logs"
    # Correction hint reached the model.
    flat = " ".join(m["content"] for m in calls["messages_seen"][-1])
    assert "intent" in flat


def test_invalid_intent_exhaustion_errors(store):
    session = store.create()
    llm, calls = make_llm(["I refuse to speak JSON."])
    events = collect(run_agent_turn(
        "how many?", session=session, call_llm=llm,
    ))
    assert events_of(events, "error")
    # Initial call + 3 reflection retries.
    assert calls["n"] == MAX_REFLECT_RETRIES + 1
    assert session.pending_sql is None


# ---- compression integration ----------------------------------------------


def test_turn_start_compresses_history(store):
    """Requirement 3/4: before generating, an oversized context is
    compressed (strategy A keeps the last 5 turns)."""
    session = store.create()
    for i in range(12):
        session.messages += [
            {"role": "user", "content": f"old question {i} " + "x" * 300},
            {"role": "assistant", "content": intent_sql(f"SELECT {i} AS n FROM audit_logs")},
            {"role": "tool", "content": json.dumps({"rows": [{"n": i}]})},
        ]
    original_len = len(session.messages)

    llm, calls = make_llm([intent_sql("SELECT COUNT(*) AS n FROM audit_logs")])
    events = collect(run_agent_turn(
        "how many?", session=session, call_llm=llm, threshold_tokens=1100,
    ))
    assert events_of(events, "confirmation_required")
    # History was compressed: fewer messages than before, recent turns kept.
    # (Tail after the turn: … user question, assistant intent JSON.)
    assert len(session.messages) < original_len
    assert session.messages[-2]["content"] == "how many?"
    # The LLM saw the compressed context, not the raw one.
    seen = calls["messages_seen"][0]
    assert len(seen) < original_len + 1
    assert any(m.get("role") == "compressed" for m in seen)


def test_turn_start_uses_llm_summary_when_needed(store):
    """Requirement 4b: strategy A insufficient → LLM summary with the
    mandated slots, keeping the last 5 turns."""
    session = store.create()
    for i in range(12):
        session.messages += [
            {"role": "user", "content": f"old question {i} " + "x" * 300},
            {"role": "assistant", "content": intent_sql(f"SELECT {i} AS n FROM audit_logs")},
        ]

    summaries = []

    def fake_summary(text):
        summaries.append(text)
        return "用户需求: 统计。已执行步骤: 12次查询。未执行步骤: 无。用户偏好: 无。"

    llm, calls = make_llm([intent_sql("SELECT COUNT(*) AS n FROM audit_logs")])
    events = collect(run_agent_turn(
        "how many?", session=session, call_llm=llm, threshold_tokens=1,
        summarize_fn=fake_summary,
    ))
    assert events_of(events, "confirmation_required")
    assert len(summaries) == 1
    assert session.messages[0]["role"] == "summary"
    assert "用户需求" in session.messages[0]["content"]
    # Last 5 turns kept verbatim after the summary.
    assert session.messages[1]["content"] == "old question 7 " + "x" * 300


# ---- audit ------------------------------------------------------------------


def test_result_recorded_in_agent_queries(store):
    session = store.create()
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    llm, _ = make_llm([intent_sql(sql)])
    collect(run_agent_turn("how many?", session=session, call_llm=llm, user="alice"))
    collect(confirm_agent_turn(session, approved=True, call_llm=llm))

    from gateway.database import query_agent_queries
    items = query_agent_queries({"page": 1, "page_size": 10})["items"]
    assert len(items) == 1
    assert items[0]["status"] == "success"
    assert items[0]["sql"] == sql
    assert items[0]["user"] == "alice"


def test_blocked_exhaustion_recorded_as_error(store):
    session = store.create()
    llm, _ = make_llm([json.dumps({"intent": "query_data", "sql": "DROP TABLE x"})])
    collect(run_agent_turn("wipe", session=session, call_llm=llm, user="bob"))

    from gateway.database import query_agent_queries
    items = query_agent_queries({"page": 1, "page_size": 10})["items"]
    assert len(items) == 1
    assert items[0]["status"] == "blocked"


# ---- knowledge prompt injection (glossary + QA few-shot) ------------------


def test_system_prompt_includes_glossary_and_qa(monkeypatch):
    from gateway import glossary, qa
    from gateway.agent_loop import _build_system_prompt as loop_prompt

    monkeypatch.setattr(
        glossary, "list_glossary_entries",
        lambda **kw: [{
            "table_name": "audit_logs", "column_name": "status",
            "business_name": "HTTP 状态码", "synonyms": '["状态"]',
            "description": "200=成功", "enabled": True,
        }],
    )
    monkeypatch.setattr(
        qa, "match_qa_pairs",
        lambda question, limit=3: [{
            "question": "上个月销售额是多少",
            "sql_template": "SELECT COUNT(*) AS n FROM audit_logs",
        }],
    )
    text = loop_prompt(question="统计上个月的销售额")
    assert "audit_logs.status" in text
    assert "HTTP 状态码" in text
    assert "上个月销售额是多少" in text
    assert "SELECT COUNT(*) AS n FROM audit_logs" in text


def test_current_question_skips_feedback_markers():
    from gateway.agent_loop import _current_question
    messages = [
        {"role": "user", "content": "how many?"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "[feedback] invalid JSON"},
        {"role": "user", "content": "[tool error] no such column"},
    ]
    assert _current_question(messages) == "how many?"
    assert _current_question([]) is None


# ---- query rewrite --------------------------------------------------------


ROUTING = {
    "upstream_url": "https://api.openai.com/v1",
    "upstream_api_key": "k",
    "upstream_auth_type": "bearer",
    "default_model": "m",
    "api_type": "openai",
}


def test_rewrite_runs_when_history_and_routing(store, monkeypatch):
    """A follow-up question in a multi-turn session is rewritten before the
    intent loop, and the model sees the rewritten (self-contained) text."""
    session = store.create()
    session.messages = [
        {"role": "user", "content": "how many requests yesterday?"},
        {"role": "assistant", "content": intent_sql("SELECT COUNT(*) AS n FROM audit_logs")},
        {"role": "tool", "content": json.dumps({"rows": [{"n": 5}]})},
    ]
    rewritten_calls = []

    async def fake_rewrite(question, messages, routing):
        rewritten_calls.append((question, routing))
        return "How many requests were there on 2026-08-26?"

    monkeypatch.setattr("gateway.agent_loop.rewrite_question", fake_rewrite)
    llm, calls = make_llm([intent_sql("SELECT COUNT(*) AS n FROM audit_logs")])

    events = collect(run_agent_turn(
        "how many were there then?",
        session=session, call_llm=llm, routing=ROUTING,
    ))
    rewrite = events_of(events, "rewrite")[0]["data"]
    assert rewrite["original"] == "how many were there then?"
    assert rewrite["rewritten"] == "How many requests were there on 2026-08-26?"
    # The model saw the rewritten question, not the original.
    seen = calls["messages_seen"][0]
    assert any(
        "How many requests were there on 2026-08-26?" in m.get("content", "")
        for m in seen
    )
    assert len(rewritten_calls) == 1


def test_rewrite_skipped_without_history(store, monkeypatch):
    session = store.create()

    async def fake_rewrite(question, messages, routing):
        raise AssertionError("rewrite must not run on a fresh session")

    monkeypatch.setattr("gateway.agent_loop.rewrite_question", fake_rewrite)
    llm, _ = make_llm([intent_sql("SELECT COUNT(*) AS n FROM audit_logs")])
    events = collect(run_agent_turn(
        "how many?", session=session, call_llm=llm, routing=ROUTING,
    ))
    assert events_of(events, "rewrite") == []


def test_rewrite_skipped_without_routing(store, monkeypatch):
    """Tests drive call_llm without routing — no upstream rewrite call."""
    session = store.create()
    session.messages = [{"role": "user", "content": "old question"}]

    async def fake_rewrite(question, messages, routing):
        raise AssertionError("rewrite must not run without routing")

    monkeypatch.setattr("gateway.agent_loop.rewrite_question", fake_rewrite)
    llm, _ = make_llm([intent_sql("SELECT COUNT(*) AS n FROM audit_logs")])
    events = collect(run_agent_turn(
        "how many?", session=session, call_llm=llm,
    ))
    assert events_of(events, "rewrite") == []


def test_rewrite_no_event_when_rewritten_equals_original(store, monkeypatch):
    session = store.create()
    session.messages = [{"role": "user", "content": "old question"}]

    async def fake_rewrite(question, messages, routing):
        return question

    monkeypatch.setattr("gateway.agent_loop.rewrite_question", fake_rewrite)
    llm, _ = make_llm([intent_sql("SELECT COUNT(*) AS n FROM audit_logs")])
    events = collect(run_agent_turn(
        "how many?", session=session, call_llm=llm, routing=ROUTING,
    ))
    assert events_of(events, "rewrite") == []
    # The original question was appended (no rewrite event, no replacement).
    assert any(
        m.get("content") == "how many?" and m.get("role") == "user"
        for m in session.messages
    )


def test_rewrite_question_openai_payload(monkeypatch):
    from gateway.agent_loop import rewrite_question
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Rewritten question?"}}]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        calls.append((url, headers, json))
        return FakeResponse()

    monkeypatch.setattr("gateway.agent_loop.httpx.AsyncClient.post", fake_post)
    result = asyncio.run(rewrite_question(
        "how many were there?",
        [{"role": "user", "content": "old question"}],
        ROUTING,
    ))
    assert result == "Rewritten question?"
    assert calls[0][0].endswith("/chat/completions")
    assert "Conversation history" in calls[0][2]["messages"][1]["content"]


def test_rewrite_question_falls_back_on_error(monkeypatch):
    from gateway.agent_loop import rewrite_question

    async def fake_post(self, url, *, headers=None, json=None, timeout=None):
        raise RuntimeError("upstream down")

    monkeypatch.setattr("gateway.agent_loop.httpx.AsyncClient.post", fake_post)
    result = asyncio.run(rewrite_question(
        "how many?",
        [{"role": "user", "content": "old question"}],
        ROUTING,
    ))
    assert result == "how many?"
