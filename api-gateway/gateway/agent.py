"""NL-to-SQL data agent for the audit log.

The agent sends the audit schema plus the user's natural-language question
to the gateway's configured upstream LLM (OpenAI-compatible chat/completions),
extracts a single SELECT statement, and executes it through the read-only
query executor. The upstream call reuses the gateway's own configured
credential (UPSTREAM_URL / UPSTREAM_API_KEY), so the agent speaks to the
same providers the gateway trusts.

Every interaction (question, generated SQL, success/error, token usage) is
recorded in the ``agent_queries`` audit table (P0-1), and the executor runs
with sensitive request/response body columns blocked.
"""

import json
import re
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import httpx

import gateway.config
from gateway.database import insert_agent_query
from gateway.glossary import render_glossary_prompt
from gateway.query import execute_readonly_query, SCHEMA_DESCRIPTIONS
from gateway.qa import render_qa_prompt
from gateway.terms import render_metric_prompt

# Correction rounds after the first generation when the generated SQL fails
# to execute (P1-3). Total upstream calls per question = 1 + MAX_SQL_RETRIES.
MAX_SQL_RETRIES = 2

_SYSTEM_PROMPT = """You are a read-only analytics assistant for an LLM API gateway audit log.
You translate user questions into a single SQLite SELECT statement.

Today's date is {today} (UTC). Relative ranges like "last 7 days" must be
resolved against this date.

Schema:
- Table audit_logs: {audit_logs}
- View v_audit_agent: {v_audit_agent}
- View v_audit_daily: {v_audit_daily}

Rules:
- Output ONLY a single SELECT statement (WITH ... SELECT CTEs allowed), no explanation.
- Never write to the database; read-only SELECT/WITH only.
- Use the v_audit_agent view for row-level questions (it has no body columns).
- Prefer the v_audit_daily view for date-aggregated questions.
- Use double quotes for identifiers (SQLite), e.g. "user".
- Group daily series as substr(timestamp, 1, 10).
- "Total tokens" always means prompt + completion + cache_read + cache_creation (provider-reported volume; cache reads dominate agent traffic and must be counted). Aggregate EACH column separately to match the dashboard: COALESCE(SUM(prompt_tokens), 0) + COALESCE(SUM(completion_tokens), 0) + COALESCE(SUM(cache_read_tokens), 0) + COALESCE(SUM(cache_creation_tokens), 0). NEVER write SUM(prompt_tokens + completion_tokens + cache_read_tokens + cache_creation_tokens) — a NULL in any column (older rows have NULL cache columns; non-LLM requests have NULL usage) drops the whole row silently. For per-row values use COALESCE on each column.
- You may use aggregate functions: COUNT, SUM, AVG, MIN, MAX.
"""


def _extract_sql_from_response(text: str | None) -> str | None:
    """Pull a single SELECT statement out of a model response."""
    if not text:
        return None
    m = re.search(r"```(?:sql)?\s*((?:SELECT|WITH)[\s\S]*?)```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(";")
    m = re.search(r"((?:SELECT|WITH)[\s\S]*?);?\s*$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(";")
    return None


def _build_system_prompt(question: str | None = None) -> str:
    """Render the system prompt with today's UTC date (P0-2).

    ``question`` enables context-sensitive few-shot injection: enabled QA
    pairs whose question overlaps the current user question are appended,
    and every enabled column-glossary annotation is included.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    base = _SYSTEM_PROMPT.format(today=today, **SCHEMA_DESCRIPTIONS)
    metric_prompt = render_metric_prompt()
    if metric_prompt:
        base += "\n\n" + metric_prompt
    glossary_prompt = render_glossary_prompt()
    if glossary_prompt:
        base += "\n\n" + glossary_prompt
    qa_prompt = render_qa_prompt(question)
    if qa_prompt:
        base += "\n\n" + qa_prompt
    return base


def _extract_usage(data: dict) -> dict:
    """Normalise upstream usage to prompt_tokens/completion_tokens.

    OpenAI chat/completions and Anthropic messages use different field
    names; both are mapped to the same keys used by the audit log.
    """
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        return {}
    result: dict = {}
    if "prompt_tokens" in usage:
        result["prompt_tokens"] = usage["prompt_tokens"]
    elif "input_tokens" in usage:
        result["prompt_tokens"] = usage["input_tokens"]
    if "completion_tokens" in usage:
        result["completion_tokens"] = usage["completion_tokens"]
    elif "output_tokens" in usage:
        result["completion_tokens"] = usage["output_tokens"]
    return result


def _record_query(
    *,
    user: str,
    question: str,
    sql: str | None,
    status: str,
    error: str | None = None,
    rows_returned: int | None = None,
    duration_ms: float,
    usage: dict | None = None,
    model: str | None = None,
    user_id: str | None = None,
) -> None:
    """Persist one agent interaction to the agent_queries audit table."""
    usage = usage or {}
    try:
        insert_agent_query(
            timestamp=datetime.now(timezone.utc).isoformat(),
            user=user,
            user_id=user_id,
            question=question,
            sql=sql,
            error=error,
            rows_returned=rows_returned,
            duration_ms=round(duration_ms, 2),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            model=model,
            status=status,
        )
    except Exception:
        # Audit persistence must never break the agent response path.
        pass


_ANTHROPIC_SUFFIX = "/anthropic"


def _apply_upstream_auth(headers: dict, api_key: str, auth_type: str) -> None:
    """Apply the upstream credential the same way the gateway proxy does
    (gateway.main._apply_upstream_auth): bearer / api-key / x-api-key."""
    if not api_key:
        return
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_type == "api-key":
        headers["api-key"] = api_key
    else:
        headers["x-api-key"] = api_key


def _routing_config(routing: dict | None) -> dict:
    """Resolve upstream call config: the signed-in user's LLM config when
    provided, otherwise the gateway-wide UPSTREAM_* env config."""
    if routing:
        return {
            "upstream_url": str(routing.get("upstream_url") or gateway.config.UPSTREAM_URL),
            "upstream_api_key": str(routing.get("upstream_api_key") or gateway.config.UPSTREAM_API_KEY),
            "upstream_auth_type": str(routing.get("upstream_auth_type") or gateway.config.UPSTREAM_AUTH_TYPE),
            "default_model": str(routing.get("default_model") or gateway.config.UPSTREAM_MODEL),
            "api_type": str(routing.get("api_type") or ""),
        }
    return {
        "upstream_url": gateway.config.UPSTREAM_URL,
        "upstream_api_key": gateway.config.UPSTREAM_API_KEY,
        "upstream_auth_type": gateway.config.UPSTREAM_AUTH_TYPE,
        "default_model": gateway.config.UPSTREAM_MODEL,
        "api_type": "",
    }


_MAX_HISTORY_TURNS = 5


def _build_user_message(
    question: str, history: list[dict] | None = None,
) -> str:
    """Compose the user message, prefixing recent question/SQL turns so
    follow-up questions resolve against earlier queries (P2-6)."""
    if not history:
        return question
    lines = ["Previous questions and their generated SQL (context only):"]
    for turn in history[-_MAX_HISTORY_TURNS:]:
        q = (turn.get("question") or "").strip()
        sql = turn.get("sql") or turn.get("error") or ""
        if q:
            lines.append(f"Q: {q}\nSQL: {sql}")
    lines.append(f"\nCurrent question: {question}")
    return "\n".join(lines)


async def _call_upstream(
    question: str,
    routing: dict | None = None,
    *,
    previous_attempts: list[dict] | None = None,
    history: list[dict] | None = None,
) -> tuple[str, dict, str]:
    """Call the signed-in user's (or gateway's) configured upstream LLM.

    OpenAI-compatible /chat/completions by default, and the Anthropic
    Messages API when the user's api_type is anthropic or the upstream URL
    points at an Anthropic-compatible base (e.g.
    https://api.deepseek.com/anthropic with x-api-key auth).

    Returns ``(content, usage, model)`` where ``usage`` is normalised to
    prompt_tokens/completion_tokens. ``previous_attempts`` (P1-3) carries
    failed SQL + error pairs so the model can correct itself.
    """
    cfg = _routing_config(routing)
    base = cfg["upstream_url"].rstrip("/")
    auth_type = cfg["upstream_auth_type"]
    api_key = cfg["upstream_api_key"]
    model = cfg["default_model"]

    is_anthropic = (
        cfg["api_type"].lower() == "anthropic"
        or base.endswith(_ANTHROPIC_SUFFIX)
    )

    user_message = _build_user_message(question, history)
    if previous_attempts:
        user_message = _build_retry_message(user_message, previous_attempts)

    if is_anthropic:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": 1024,
            "system": _build_system_prompt(question),
            "messages": [{"role": "user", "content": user_message}],
        }
        _apply_upstream_auth(headers, api_key, auth_type)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base}/v1/messages", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        try:
            content = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
        except (KeyError, IndexError, TypeError):
            raise ValueError("Upstream LLM returned an unexpected response")
        return content, _extract_usage(data), model

    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _build_system_prompt(question)},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0,
    }
    _apply_upstream_auth(headers, api_key, auth_type)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Upstream LLM returned an unexpected response")
    return content, _extract_usage(data), model


def _build_retry_message(
    question: str, previous_attempts: list[dict],
) -> str:
    """Compose the user message for a correction round (P1-3)."""
    parts = [question]
    for attempt in previous_attempts:
        parts.append(
            f"\n\nThe SQL you generated failed to execute:\n"
            f"```sql\n{attempt.get('sql', '')}\n```\n"
            f"Error: {attempt.get('error', '')}\n"
            f"Please fix the SQL and output ONLY the corrected read-only "
            f"SELECT (or WITH ... SELECT) statement."
        )
    return "\n".join(parts)


async def _stream_upstream(
    question: str,
    routing: dict | None = None,
    *,
    previous_attempts: list[dict] | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator[str]:
    """Yield text deltas from Anthropic- or OpenAI-compatible SSE streams."""
    cfg = _routing_config(routing)
    base = cfg["upstream_url"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    _apply_upstream_auth(
        headers, cfg["upstream_api_key"], cfg["upstream_auth_type"],
    )

    user_message = _build_user_message(question, history)
    if previous_attempts:
        user_message = _build_retry_message(user_message, previous_attempts)

    is_anthropic = (
        cfg["api_type"].lower() == "anthropic"
        or base.endswith(_ANTHROPIC_SUFFIX)
    )
    if is_anthropic:
        headers["anthropic-version"] = "2023-06-01"
        url = f"{base}/v1/messages"
        payload = {
            "model": cfg["default_model"],
            "max_tokens": 1024,
            "system": _build_system_prompt(question),
            "messages": [{"role": "user", "content": user_message}],
            "stream": True,
        }
    else:
        url = f"{base}/chat/completions"
        payload = {
            "model": cfg["default_model"],
            "messages": [
                {"role": "system", "content": _build_system_prompt(question)},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0,
            "stream": True,
        }

    timeout = httpx.Timeout(30.0, read=300.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", url, headers=headers, json=payload,
        ) as response:
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


async def stream_agent(
    question: str,
    routing: dict | None = None,
    max_rows: int = 100,
    user: str = "-",
    user_id: str | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Stream model deltas, then execute and emit the completed SQL result.

    The interaction is recorded in the ``agent_queries`` audit table and the
    query executes with sensitive body columns blocked (P0-1).
    """
    if not question or not question.strip():
        raise ValueError("Question is empty")

    started = time.perf_counter()
    attempts: list[dict] = []
    usage: dict = {}
    model: str | None = None
    sql: str | None = None

    for round_no in range(MAX_SQL_RETRIES + 1):
        if round_no == 0:
            yield {
                "event": "status",
                "data": {"stage": "generating", "message": "Generating SQL…"},
            }
        else:
            yield {
                "event": "status",
                "data": {
                    "stage": "retrying",
                    "message": "SQL failed — asking the model to fix it…",
                },
            }
        parts: list[str] = []
        async for text in _stream_upstream(
            question, routing,
            previous_attempts=attempts or None,
            history=history,
        ):
            parts.append(text)
            if round_no == 0:
                yield {"event": "delta", "data": {"text": text}}

        sql = _extract_sql_from_response("".join(parts))
        if sql is None:
            _record_query(
                user=user, user_id=user_id, question=question, sql=None,
                status="error",
                error="Could not extract SQL from the model response",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise ValueError("Could not extract SQL from the model response")

        if round_no == 0:
            yield {"event": "sql", "data": {"sql": sql}}
        try:
            result = execute_readonly_query(
                sql, max_rows=max_rows, allow_sensitive_columns=False,
            )
            break
        except Exception as exc:
            attempts.append({"sql": sql, "error": str(exc)})
            last_error = exc
    else:
        _record_query(
            user=user, user_id=user_id, question=question, sql=sql,
            status="error", error=str(last_error),
            duration_ms=(time.perf_counter() - started) * 1000,
            usage=usage, model=model,
        )
        raise last_error

    # After a correction round the client already saw the first SQL; emit the
    # corrected statement so the visible SQL matches the executed result.
    if round_no > 0:
        yield {"event": "sql", "data": {"sql": sql}}
    yield {
        "event": "status",
        "data": {"stage": "executing", "message": "Executing read-only query…"},
    }
    _record_query(
        user=user, user_id=user_id, question=question, sql=sql,
        status="success", rows_returned=len(result["rows"]),
        duration_ms=(time.perf_counter() - started) * 1000,
        usage=usage, model=model,
    )
    yield {"event": "result", "data": {"sql": sql, **result}}
    yield {"event": "done", "data": {}}


async def ask_agent(
    question: str,
    routing: dict | None = None,
    max_rows: int = 100,
    user: str = "-",
    user_id: str | None = None,
    history: list[dict] | None = None,
) -> dict:
    """Translate a question to SQL, execute it read-only, return results.

    ``routing`` is the signed-in user's LLM config (upstream_url / api key /
    auth type / default model). When omitted, the gateway-wide UPSTREAM_*
    env config is used (legacy fallback).

    The interaction is recorded in the ``agent_queries`` audit table and the
    query executes with sensitive body columns blocked (P0-1).
    """
    if not question or not question.strip():
        raise ValueError("Question is empty")
    started = time.perf_counter()
    attempts: list[dict] = []
    for round_no in range(MAX_SQL_RETRIES + 1):
        content, usage, model = await _call_upstream(
            question, routing,
            previous_attempts=attempts or None,
            history=history,
        )
        sql = _extract_sql_from_response(content)
        if sql is None:
            _record_query(
                user=user, user_id=user_id, question=question, sql=None,
                status="error",
                error="Could not extract SQL from the model response",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise ValueError("Could not extract SQL from the model response")
        try:
            result = execute_readonly_query(
                sql, max_rows=max_rows, allow_sensitive_columns=False,
            )
            break
        except Exception as exc:
            attempts.append({"sql": sql, "error": str(exc)})
            last_error = exc
    else:
        _record_query(
            user=user, user_id=user_id, question=question, sql=sql,
            status="error", error=str(last_error),
            duration_ms=(time.perf_counter() - started) * 1000,
            usage=usage, model=model,
        )
        raise last_error
    _record_query(
        user=user, user_id=user_id, question=question, sql=sql,
        status="success", rows_returned=len(result["rows"]),
        duration_ms=(time.perf_counter() - started) * 1000,
        usage=usage, model=model,
    )
    return {"sql": sql, **result}
