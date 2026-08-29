"""NL2SQL golden regression suite (improvement 4).

Locks the *protocol-level* behaviour of the agent pipeline end-to-end with
an injected LLM: system-prompt knowledge injection, intent parsing,
sqlguard blocking + reflection retry, confirmation and execution against
the seeded audit DB. Prompt/glossary/QA/guard changes that alter these
golden traces fail here — the LLM itself is evaluated out-of-band via
``scripts/agent_eval.py``.
"""

import asyncio
import json
import os
import tempfile

import pytest

import gateway.config as config
from gateway.agent_loop import run_agent_turn, confirm_agent_turn
from gateway.database import init_audit_db, insert_log
from gateway.glossary import upsert_glossary_entry
from gateway.qa import upsert_qa_pair
from gateway.session import SessionStore
from gateway.terms import upsert_metric_term


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="golden_test_")
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
    calls = {"n": 0, "messages_seen": []}

    async def call_llm(messages, routing=None):
        calls["messages_seen"].append([dict(m) for m in messages])
        text = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        yield text

    return call_llm, calls


def collect(agen):
    return asyncio.run(_drain(agen))


async def _drain(agen):
    return [event async for event in agen]


def events_of(events, name):
    return [e for e in events if e["event"] == name]


def _intent(sql=None, **extra):
    payload = {"intent": "query_data", "sql": sql} if sql else extra
    return json.dumps(payload, ensure_ascii=False)


def run_turn(store, question, responses):
    llm, calls = make_llm(responses)
    session = store.create()
    events = collect(run_agent_turn(question, session=session, call_llm=llm))
    return session, events, calls


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------

def test_golden_count_query_confirm_execute(store):
    """Simple aggregate: confirmation carries the exact SQL; approval
    executes it against the audit DB and returns real rows."""
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    session, events, calls = run_turn(
        store, "总请求数是多少？", [_intent(sql)],
    )
    confirm = events_of(events, "confirmation_required")[0]
    assert confirm["data"]["sql"] == sql
    assert session.pending_sql == sql
    # Not executed before confirmation.
    assert events_of(events, "result") == []

    llm, _ = make_llm([])
    done = collect(confirm_agent_turn(session, approved=True, call_llm=llm))
    result = events_of(done, "result")[0]
    assert result["data"]["rows"] == [{"n": 1}]


def test_golden_write_sql_blocked_then_retry(store):
    """A DELETE proposal is blocked by sqlguard, the model is re-asked with
    feedback, and only the second (safe) SQL reaches confirmation."""
    bad = "DELETE FROM audit_logs WHERE 1=1"
    good = "SELECT COUNT(*) AS n FROM audit_logs WHERE level = 'ERROR'"
    session, events, calls = run_turn(
        store, "帮我删掉昨天的日志",
        [_intent(bad), _intent(good)],
    )
    blocked = events_of(events, "blocked")
    assert blocked and blocked[0]["data"]["sql"] == bad
    assert "retrying" in [e["event"] for e in events]
    confirm = events_of(events, "confirmation_required")[0]
    assert confirm["data"]["sql"] == good
    assert confirm["data"]["attempt"] == 2
    # The dangerous statement never reaches the executor or pending state.
    assert session.pending_sql == good
    assert events_of(events, "result") == []


def test_golden_chitchat_produces_no_sql(store):
    session, events, calls = run_turn(
        store, "你是谁？",
        [_intent(intent="chitchat", message="我是网关数据助手")],
    )
    names = [e["event"] for e in events]
    assert "confirmation_required" not in names
    assert events_of(events, "message")[0]["data"]["message"] == "我是网关数据助手"


def test_golden_fenced_json_intent_still_parses(store):
    """Tolerant extraction: a markdown-fenced intent JSON block is accepted."""
    sql = "SELECT 1 AS one"
    fenced = f"```json\n{_intent(sql)}\n```\n"
    session, events, calls = run_turn(store, "跑个 1", [fenced])
    confirm = events_of(events, "confirmation_required")[0]
    assert confirm["data"]["sql"] == sql


def test_golden_null_safe_aggregate_passes_guard(store):
    """The canonical NULL-safe total form must pass the guard untouched."""
    sql = ("SELECT COALESCE(SUM(prompt_tokens), 0) + "
           "COALESCE(SUM(completion_tokens), 0) AS total_tokens "
           "FROM audit_logs")
    session, events, calls = run_turn(store, "总 token 数", [_intent(sql)])
    assert events_of(events, "blocked") == []
    assert events_of(events, "confirmation_required")[0]["data"]["sql"] == sql


def test_golden_cte_passes_guard(store):
    sql = ("WITH daily AS (SELECT substr(timestamp, 1, 10) AS d, COUNT(*) AS c "
           "FROM audit_logs GROUP BY d) SELECT * FROM daily ORDER BY d")
    session, events, calls = run_turn(store, "按天统计请求", [_intent(sql)])
    assert events_of(events, "blocked") == []
    assert events_of(events, "confirmation_required")[0]["data"]["sql"] == sql


def test_golden_chart_suggestion_flows_to_result(store):
    sql = "SELECT COUNT(*) AS n FROM audit_logs"
    chart = {"type": "bar", "x": "n", "series": "count"}
    out = json.dumps({
        "intent": "query_data", "sql": sql, "chart": chart,
    })
    session, events, calls = run_turn(store, "请求数柱状图", [out])
    # The chart slot survives parsing and parks on the pending state.
    assert session.pending_chart == chart

    llm, _ = make_llm([])
    done = collect(confirm_agent_turn(session, approved=True, call_llm=llm))
    assert events_of(done, "result")[0]["data"]["chart"] == chart
    assert session.pending_chart is None


# ---------------------------------------------------------------------------
# Knowledge injection regression (glossary / QA / metric terms)
# ---------------------------------------------------------------------------

def test_golden_knowledge_reaches_system_prompt(store):
    """The assembled system prompt must carry all three knowledge sources:
    metric-term dictionary, column glossary, and question-matched few-shot
    QA. (Injected test call_llm's bypass the transport, so the builder is
    exercised directly — same function the real stream uses.)"""
    from gateway.agent_loop import _build_system_prompt

    upsert_metric_term("有效请求", "status < 400 的请求", ["ok req"])
    upsert_glossary_entry("prompt_cache_read_input_tokens",
                          "命中缓存读取的输入 token")
    upsert_qa_pair("上周错误率",
                   "SELECT COUNT(*) FILTER (WHERE status>=500) FROM audit_logs")
    prompt = _build_system_prompt(question="上周错误率是多少")
    assert "有效请求" in prompt          # metric-term dictionary
    assert "命中缓存读取的输入 token" in prompt  # column glossary
    assert "上周错误率" in prompt        # few-shot QA matched by question


def test_golden_auto_line_chart_inferred_when_omitted(store):
    """Time-series-shaped results get a line chart even if the model
    omitted the chart slot (36343e6)."""
    insert_log(
        timestamp="2026-08-02T10:00:00+00:00",
        user="bob", method="POST", path="/v1/messages",
        status=200, duration_ms=300.0, upstream_ms=250.0, level="INFO",
        prompt_tokens=10, completion_tokens=5, model="m",
    )
    sql = ("SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS c "
           "FROM audit_logs GROUP BY day")
    session, events, calls = run_turn(store, "按天趋势", [_intent(sql)])
    llm, _ = make_llm([])
    done = collect(confirm_agent_turn(session, approved=True, call_llm=llm))
    chart = events_of(done, "result")[0]["data"].get("chart")
    assert chart == {"type": "line", "x": "day", "series": "c"}
