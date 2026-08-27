"""Stateful agent loop for the data agent.

Turn model (requirement 1): the server owns the conversation
(:mod:`gateway.session`). Each user question runs through an intent loop:

1. Compress the context when it overflows the token budget
   (requirement 3/4, :mod:`gateway.compression`).
2. Ask the LLM for a strict intent-slot JSON answer
   (requirement 8, :mod:`gateway.intent`).
3. ``chitchat``/``clarify`` replies are returned directly.
4. ``query_data`` SQL is first screened by :mod:`gateway.sqlguard`
   (requirement 9); safe SQL is NEVER executed automatically — the loop
   emits ``confirmation_required`` and waits for the human decision
   (requirement 10).
5. Execution errors and blocked SQL are fed back to the model for
   reflection, at most :data:`MAX_REFLECT_RETRIES` times
   (requirement 2); every regenerated SQL requires a fresh confirmation.

Events emitted (SSE-friendly dicts): ``session``, ``status``, ``delta``,
``intent``, ``message``, ``retrying``, ``blocked``,
``confirmation_required``, ``result``, ``rejected``, ``error``, ``done``.
"""

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx

from gateway.agent import _apply_upstream_auth, _routing_config
from gateway.compression import (
    SUMMARY_TOKEN_THRESHOLD,
    apply_summary,
    compress_context,
    summarize_via_llm,
)
from gateway.intent import parse_intent
from gateway.query import SCHEMA_DESCRIPTIONS, execute_readonly_query
from gateway.sqlguard import check_sql_safety
from gateway.terms import render_metric_prompt

MAX_REFLECT_RETRIES = 3
_ANTHROPIC_SUFFIX = "/anthropic"

_SYSTEM_PROMPT = """You are a read-only data agent for an LLM API gateway audit log.

You MUST answer with a single JSON object and nothing else:
{{"intent": "query_data" | "chitchat" | "clarify", "sql": "<only for query_data>", "message": "<only for chitchat/clarify>", "chart": "<optional for query_data>"}}

- intent "query_data": translate the question into exactly one SQLite SELECT in "sql".
- intent "chitchat": small talk; answer in "message".
- intent "clarify": the question is ambiguous; ask in "message".
- Never generate write statements; SELECT only. Output valid JSON only.
- Optional "chart" for query_data when the result is naturally visualised (trends, rankings, distributions): {{"type": "bar" | "line" | "pie", "x": "<column for x-axis/categories>", "series": "<numeric column for values>"}}. "x" and "series" MUST be real columns of the SELECT result. Omit "chart" when a table alone is clearer.

Today's date is {today} (UTC). Resolve relative ranges against it.

Schema:
- Table audit_logs: {audit_logs}
- View v_audit_agent: {v_audit_agent}
- View v_audit_daily: {v_audit_daily}

Rules:
- Use the v_audit_agent view for row-level questions (no body columns).
- Prefer v_audit_daily for date-aggregated questions.
- Use double quotes for identifiers (SQLite), e.g. "user".
- Group daily series as substr(timestamp, 1, 10).
"""


def _build_system_prompt() -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    base = _SYSTEM_PROMPT.format(today=today, **SCHEMA_DESCRIPTIONS)
    metric_prompt = render_metric_prompt()
    if metric_prompt:
        base += "\n\n" + metric_prompt
    return base


def _default_execute(sql: str) -> dict:
    return execute_readonly_query(sql, max_rows=100, allow_sensitive_columns=False)


_DATE_COLUMN_HINTS = frozenset({"date", "day", "dt", "timestamp", "time", "datetime"})


def _infer_chart_from_result(result: dict) -> dict | None:
    """Infer a line chart when the result looks like a time series.

    Fallback for when the model omitted a ``chart`` suggestion but the
    query result contains an obvious date column plus a numeric column
    (e.g. daily request counts). Returns None when no suitable columns
    exist so plain tables stay plain.
    """
    columns = result.get("columns") or []
    rows = result.get("rows") or []
    if not columns or not rows or not isinstance(rows[0], dict):
        return None
    lowered = {c: c.lower() for c in columns}
    date_col = next(
        (c for c in columns if lowered[c] in _DATE_COLUMN_HINTS or "date" in lowered[c]),
        None,
    )
    if date_col is None:
        return None
    for c in columns:
        if c == date_col:
            continue
        try:
            float(rows[0].get(c))
        except (TypeError, ValueError):
            continue
        return {"type": "line", "x": date_col, "series": c}
    return None


def _build_llm_messages(session, extra: list[dict] | None = None) -> list[dict]:
    """Session history + current question as LLM messages (raw roles).

    Roles ``summary``/``compressed``/``tool`` stay symbolic here; the
    transport (``_stream_llm``) maps them to API-compatible roles.
    """
    messages = [dict(m) for m in session.messages]
    if extra:
        messages.extend(extra)
    return messages


def _to_api_messages(messages: list[dict], system_prompt: str):
    """Map raw roles to OpenAI chat roles and split out system text."""
    system_parts = [system_prompt]
    chat: list[dict] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "system":
            system_parts.append(content)
        elif role == "summary":
            system_parts.append(f"[Conversation summary]\n{content}")
        elif role == "compressed":
            chat.append({"role": "user", "content": f"[History record] {content}"})
        elif role == "tool":
            chat.append({"role": "user", "content": f"[Tool result]\n{content}"})
        else:
            chat.append({"role": role, "content": content})
    return "\n\n".join(system_parts), chat


async def _stream_llm(messages: list[dict], routing: dict | None = None) -> AsyncIterator[str]:
    """Default transport: stream the conversation to the configured upstream."""
    cfg = _routing_config(routing)
    base = cfg["upstream_url"].rstrip("/")
    auth_type = cfg["upstream_auth_type"]
    api_key = cfg["upstream_api_key"]
    model = cfg["default_model"]
    is_anthropic = cfg["api_type"].lower() == "anthropic" or base.endswith(_ANTHROPIC_SUFFIX)

    system_text, chat = _to_api_messages(messages, _build_system_prompt())

    if is_anthropic:
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        _apply_upstream_auth(headers, api_key, auth_type)
        url = f"{base}/v1/messages"
        payload = {
            "model": model,
            "max_tokens": 2048,
            "system": system_text,
            "messages": chat,
            "stream": True,
        }
    else:
        headers = {"Content-Type": "application/json"}
        _apply_upstream_auth(headers, api_key, auth_type)
        url = f"{base}/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_text}] + chat,
            "temperature": 0,
            "stream": True,
        }

    timeout = httpx.Timeout(30.0, read=300.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                text = ""
                if is_anthropic:
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text") or ""
                else:
                    choices = event.get("choices") or []
                    if choices:
                        text = (choices[0].get("delta") or {}).get("content") or ""
                if text:
                    yield text


async def _ensure_compressed(
    session,
    routing: dict | None,
    threshold_tokens: int | None,
    summarize_fn,
) -> None:
    """Compress session messages in place when over the token budget."""
    threshold = threshold_tokens or SUMMARY_TOKEN_THRESHOLD
    result = compress_context(session.messages, threshold_tokens=threshold)
    if result["strategy"] is None:
        return
    if result["strategy"] == "turn_compress":
        session.messages = result["messages"]
        return
    # Strategy B: whole-context LLM summary (keep the last 5 turns).
    text = result["summary_text_needed"]
    if summarize_fn is not None:
        summary = summarize_fn(text)
    else:
        summary = await summarize_via_llm(routing, text)
    session.messages = apply_summary(session.messages, summary, keep_recent_5_tokens=True)


def _record_audit(
    *, user: str, user_id: str | None, question: str, sql: str | None,
    status: str, error: str | None = None, rows_returned: int | None = None,
) -> None:
    """Persist one agent interaction; never break the response path."""
    from gateway.database import insert_agent_query
    try:
        insert_agent_query(
            timestamp=datetime.now(timezone.utc).isoformat(),
            user=user, user_id=user_id, question=question, sql=sql,
            error=error, rows_returned=rows_returned, duration_ms=0.0,
            prompt_tokens=None, completion_tokens=None, model=None,
            status=status,
        )
    except Exception:
        pass


def _generate_once(session, call_llm, routing, collect_into: list[str]):
    """One LLM round-trip as a live event stream.

    Yields status + each delta as the model produces it (no buffering);
    the full text is accumulated into ``collect_into`` for the caller.
    """
    return _generate_once_impl(session, call_llm, routing, collect_into)


async def _generate_once_impl(session, call_llm, routing, collect_into: list[str]) -> AsyncIterator[dict]:
    yield {"event": "status", "data": {"stage": "generating", "message": "Generating…"}}
    async for text in call_llm(_build_llm_messages(session), routing=routing):
        collect_into.append(text)
        yield {"event": "delta", "data": {"text": text}}


async def run_agent_turn(question, session, call_llm=None, routing=None,
                         user="-", user_id=None, execute_fn=None,
                         threshold_tokens=None, summarize_fn=None):
    """Run one user question through the intent loop until it needs a
    human SQL confirmation or finishes with a direct answer.

    Concurrent turns on the same session are serialised by the session
    lock, so messages never interleave at await points.
    """
    async with session.lock:
        async for event in _run_agent_turn_impl(
            question, session, call_llm=call_llm, routing=routing,
            user=user, user_id=user_id, execute_fn=execute_fn,
            threshold_tokens=threshold_tokens, summarize_fn=summarize_fn,
        ):
            yield event


async def _run_agent_turn_impl(
    question: str,
    session,
    call_llm: AsyncIterator | None = None,
    routing: dict | None = None,
    user: str = "-",
    user_id: str | None = None,
    execute_fn=None,  # noqa: ARG001 — reserved for symmetry with confirm
    threshold_tokens: int | None = None,
    summarize_fn=None,
) -> AsyncIterator[dict]:
    """Unlocked implementation of :func:`run_agent_turn`."""
    if call_llm is None:
        call_llm = _stream_llm
    if not question or not question.strip():
        yield {"event": "error", "data": {"message": "Question is empty"}}
        yield {"event": "done", "data": {}}
        return

    yield {"event": "session", "data": {"session_id": session.session_id}}

    await _ensure_compressed(session, routing, threshold_tokens, summarize_fn)
    session.messages.append({"role": "user", "content": question})

    attempts_used = 0
    max_attempts = MAX_REFLECT_RETRIES + 1
    while attempts_used < max_attempts:
        attempts_used += 1
        parts: list[str] = []
        async for event in _generate_once(session, call_llm, routing, parts):
            yield event
        full_text = "".join(parts)

        intent = parse_intent(full_text)
        if intent is None:
            feedback = (
                "[feedback] Your reply was not a valid intent JSON. You must answer "
                'with a single JSON object like {"intent": "query_data", "sql": "..."} '
                'or {"intent": "chitchat", "message": "..."}. Reply again with ONLY the JSON.'
            )
        elif intent["intent"] == "query_data" and not intent.get("sql"):
            intent = None
            feedback = (
                '[feedback] intent "query_data" requires a "sql" field with one SELECT '
                "statement. Reply again with ONLY the corrected JSON."
            )
        else:
            feedback = None

        if intent is None:
            if attempts_used >= max_attempts:
                session.messages.append({"role": "assistant", "content": full_text})
                _record_audit(
                    user=user, user_id=user_id, question=question, sql=None,
                    status="error",
                    error="Model did not produce a valid intent JSON",
                )
                yield {"event": "error", "data": {
                    "message": "Model did not produce a valid intent JSON",
                }}
                yield {"event": "done", "data": {}}
                return
            yield {"event": "retrying", "data": {
                "attempt": attempts_used + 1, "reason": "invalid intent JSON",
            }}
            session.messages.append({"role": "assistant", "content": full_text})
            session.messages.append({"role": "user", "content": feedback})
            continue

        yield {"event": "intent", "data": dict(intent)}

        if intent["intent"] in ("chitchat", "clarify"):
            message = intent.get("message") or ""
            session.messages.append({"role": "assistant", "content": full_text})
            yield {"event": "message", "data": {"message": message, "intent": intent["intent"]}}
            yield {"event": "done", "data": {}}
            return

        # query_data — screen the SQL before asking for confirmation.
        sql = intent["sql"]
        ok, reason = check_sql_safety(sql)
        if not ok:
            yield {"event": "blocked", "data": {"sql": sql, "reason": reason}}
            if attempts_used >= max_attempts:
                session.messages.append({"role": "assistant", "content": full_text})
                _record_audit(
                    user=user, user_id=user_id, question=question, sql=sql,
                    status="blocked", error=reason,
                )
                yield {"event": "error", "data": {
                    "message": f"Dangerous SQL was blocked: {reason}",
                }}
                yield {"event": "done", "data": {}}
                return
            yield {"event": "retrying", "data": {
                "attempt": attempts_used + 1, "reason": reason,
            }}
            session.messages.append({"role": "assistant", "content": full_text})
            session.messages.append({"role": "user", "content": (
                f"[feedback] The SQL was BLOCKED by the safety guard: {reason}. "
                "Generate ONLY a safe, read-only SELECT as intent JSON."
            )})
            continue

        # Safe SQL: never execute automatically — require human confirmation.
        session.messages.append({"role": "assistant", "content": full_text})
        session.pending_sql = sql
        session.pending_question = question
        session.pending_attempt = attempts_used
        session.pending_chart = intent.get("chart")
        session.user = user
        session.user_id = user_id
        yield {"event": "confirmation_required", "data": {
            "sql": sql, "attempt": attempts_used, "question": question,
        }}
        yield {"event": "done", "data": {}}
        return


async def confirm_agent_turn(session, approved, call_llm=None, routing=None,
                             user="-", user_id=None, execute_fn=None,
                             threshold_tokens=None, summarize_fn=None):
    """Resolve a pending SQL confirmation.

    Approved SQL executes read-only; failures trigger reflection retries
    (each regenerated SQL goes back to ``confirmation_required``).

    Concurrent confirms on the same session are serialised by the session
    lock — a second confirm sees ``pending_sql`` already cleared and
    reports that nothing is awaiting confirmation.
    """
    async with session.lock:
        async for event in _confirm_agent_turn_impl(
            session, approved, call_llm=call_llm, routing=routing,
            user=user, user_id=user_id, execute_fn=execute_fn,
            threshold_tokens=threshold_tokens, summarize_fn=summarize_fn,
        ):
            yield event


async def _confirm_agent_turn_impl(
    session,
    approved: bool,
    call_llm: AsyncIterator | None = None,
    routing: dict | None = None,
    user: str = "-",
    user_id: str | None = None,
    execute_fn=None,
    threshold_tokens: int | None = None,
    summarize_fn=None,
) -> AsyncIterator[dict]:
    """Unlocked implementation of :func:`confirm_agent_turn`."""
    if call_llm is None:
        call_llm = _stream_llm
    if execute_fn is None:
        execute_fn = _default_execute

    yield {"event": "session", "data": {"session_id": session.session_id}}

    if session.pending_sql is None:
        yield {"event": "error", "data": {"message": "No SQL is awaiting confirmation"}}
        yield {"event": "done", "data": {}}
        return

    if not approved:
        session.pending_sql = None
        session.pending_question = None
        session.pending_attempt = 0
        session.pending_chart = None
        yield {"event": "rejected", "data": {}}
        yield {"event": "done", "data": {}}
        return

    await _ensure_compressed(session, routing, threshold_tokens, summarize_fn)

    max_attempts = MAX_REFLECT_RETRIES + 1
    while True:
        sql = session.pending_sql
        question = session.pending_question or ""
        attempt = session.pending_attempt
        audit_user = getattr(session, "user", None) or user
        audit_user_id = getattr(session, "user_id", None) or user_id

        yield {"event": "status", "data": {
            "stage": "executing", "message": "Executing read-only query…",
        }}
        try:
            result = execute_fn(sql)
        except Exception as exc:
            error_text = str(exc)
            if attempt >= max_attempts:
                session.pending_sql = None
                session.pending_question = None
                session.pending_attempt = 0
                session.messages.append({"role": "user", "content": (
                    f"[tool error] Executing the SQL failed: {error_text}"
                )})
                _record_audit(
                    user=audit_user, user_id=audit_user_id, question=question,
                    sql=sql, status="error", error=error_text,
                )
                yield {"event": "error", "data": {
                    "message": f"SQL execution failed after reflection retries: {error_text}",
                }}
                yield {"event": "done", "data": {}}
                return

            # Reflection: feed the error back and generate again.
            yield {"event": "retrying", "data": {
                "attempt": attempt + 1, "reason": error_text,
            }}
            session.messages.append({"role": "user", "content": (
                f"[tool error] Executing your SQL failed: {error_text}\n"
                "```sql\n" + sql + "\n```\n"
                "Fix it and answer with ONLY the corrected intent JSON."
            )})
            session.pending_sql = None  # cleared until regeneration
            new_sql = await _reflect_for_sql(session, call_llm, routing)
            if new_sql is None:
                session.pending_question = None
                session.pending_attempt = 0
                _record_audit(
                    user=audit_user, user_id=audit_user_id, question=question,
                    sql=sql, status="error",
                    error="Reflection could not produce a valid intent JSON",
                )
                yield {"event": "error", "data": {
                    "message": "Reflection could not produce a valid intent JSON",
                }}
                yield {"event": "done", "data": {}}
                return
            session.pending_sql = new_sql
            session.pending_attempt = attempt + 1
            yield {"event": "confirmation_required", "data": {
                "sql": new_sql, "attempt": attempt + 1, "question": question,
            }}
            yield {"event": "done", "data": {}}
            return

        # Success — record, store the tool result, clear pending state.
        session.messages.append({"role": "tool", "content": json.dumps(
            {"sql": sql, "columns": result.get("columns"),
             "rows": result.get("rows"), "truncated": result.get("truncated")},
            ensure_ascii=False, default=str,
        )})
        chart = session.pending_chart
        if chart is None:
            chart = _infer_chart_from_result(result)
        session.pending_sql = None
        session.pending_question = None
        session.pending_attempt = 0
        session.pending_chart = None
        _record_audit(
            user=audit_user, user_id=audit_user_id, question=question,
            sql=sql, status="success",
            rows_returned=len(result.get("rows") or []),
        )
        result_data = {"sql": sql, **result}
        if chart is not None:
            result_data["chart"] = chart
        yield {"event": "result", "data": result_data}
        yield {"event": "done", "data": {}}
        return


async def _reflect_for_sql(session, call_llm, routing) -> str | None:
    """Generate the next attempt after an execution error.

    Returns the safe SQL string, or None when the model cannot produce a
    valid intent JSON. The attempt count is advanced by the caller via
    ``session.pending_attempt``.
    """
    parts: list[str] = []
    async for _event in _generate_once(session, call_llm, routing, parts):
        pass  # Deltas from reflection are internal — never surfaced.
    full_text = "".join(parts)
    intent = parse_intent(full_text)
    if intent is None or intent["intent"] != "query_data" or not intent.get("sql"):
        session.messages.append({"role": "assistant", "content": full_text})
        return None
    sql = intent["sql"]
    ok, reason = check_sql_safety(sql)
    session.messages.append({"role": "assistant", "content": full_text})
    if not ok:
        # A dangerous correction is treated as a failed reflection.
        session.messages.append({"role": "user", "content": (
            f"[feedback] The corrected SQL was BLOCKED: {reason}"
        )})
        return None
    return sql
