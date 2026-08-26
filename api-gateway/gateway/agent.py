"""NL-to-SQL data agent for the audit log.

The agent sends the audit schema plus the user's natural-language question
to the gateway's configured upstream LLM (OpenAI-compatible chat/completions),
extracts a single SELECT statement, and executes it through the read-only
query executor. The upstream call reuses the gateway's own configured
credential (UPSTREAM_URL / UPSTREAM_API_KEY), so the agent speaks to the
same providers the gateway trusts.
"""

import json
import re
from collections.abc import AsyncIterator

import httpx

import gateway.config
from gateway.query import execute_readonly_query, SCHEMA_DESCRIPTIONS

_SYSTEM_PROMPT = """You are a read-only analytics assistant for an LLM API gateway audit log.
You translate user questions into a single SQLite SELECT statement.

Schema:
- Table audit_logs: {audit_logs}
- View v_audit_daily: {v_audit_daily}

Rules:
- Output ONLY a single SELECT statement, no explanation.
- Never write to the database; SELECT only.
- Prefer the v_audit_daily view for date-aggregated questions.
- Use double quotes for identifiers (SQLite), e.g. "user".
- Group daily series as substr(timestamp, 1, 10).
- You may use aggregate functions: COUNT, SUM, AVG, MIN, MAX.
""".format(**SCHEMA_DESCRIPTIONS)


def _extract_sql_from_response(text: str | None) -> str | None:
    """Pull a single SELECT statement out of a model response."""
    if not text:
        return None
    m = re.search(r"```(?:sql)?\s*(SELECT[\s\S]*?)```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(";")
    m = re.search(r"(SELECT[\s\S]*?);?\s*$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(";")
    return None


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


async def _call_upstream(question: str, routing: dict | None = None) -> str:
    """Call the signed-in user's (or gateway's) configured upstream LLM.

    OpenAI-compatible /chat/completions by default, and the Anthropic
    Messages API when the user's api_type is anthropic or the upstream URL
    points at an Anthropic-compatible base (e.g.
    https://api.deepseek.com/anthropic with x-api-key auth).
    """
    cfg = _routing_config(routing)
    base = cfg["upstream_url"].rstrip("/")
    auth_type = cfg["upstream_auth_type"]
    api_key = cfg["upstream_api_key"]
    model = cfg["default_model"]

    if cfg["api_type"].lower() == "anthropic" or base.endswith(_ANTHROPIC_SUFFIX):
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": model,
            "max_tokens": 1024,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": question}],
        }
        _apply_upstream_auth(headers, api_key, auth_type)

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base}/v1/messages", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        try:
            return "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
        except (KeyError, IndexError, TypeError):
            raise ValueError("Upstream LLM returned an unexpected response")

    headers = {"Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
    }
    _apply_upstream_auth(headers, api_key, auth_type)

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{base}/chat/completions", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Upstream LLM returned an unexpected response")


async def _stream_upstream(
    question: str, routing: dict | None = None,
) -> AsyncIterator[str]:
    """Yield text deltas from Anthropic- or OpenAI-compatible SSE streams."""
    cfg = _routing_config(routing)
    base = cfg["upstream_url"].rstrip("/")
    headers = {"Content-Type": "application/json"}
    _apply_upstream_auth(
        headers, cfg["upstream_api_key"], cfg["upstream_auth_type"],
    )

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
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": question}],
            "stream": True,
        }
    else:
        url = f"{base}/chat/completions"
        payload = {
            "model": cfg["default_model"],
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": question},
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
    question: str, routing: dict | None = None, max_rows: int = 100,
) -> AsyncIterator[dict]:
    """Stream model deltas, then execute and emit the completed SQL result."""
    if not question or not question.strip():
        raise ValueError("Question is empty")

    yield {
        "event": "status",
        "data": {"stage": "generating", "message": "Generating SQL…"},
    }
    parts: list[str] = []
    async for text in _stream_upstream(question, routing):
        parts.append(text)
        yield {"event": "delta", "data": {"text": text}}

    sql = _extract_sql_from_response("".join(parts))
    if sql is None:
        raise ValueError("Could not extract SQL from the model response")

    yield {"event": "sql", "data": {"sql": sql}}
    yield {
        "event": "status",
        "data": {"stage": "executing", "message": "Executing read-only query…"},
    }
    result = execute_readonly_query(sql, max_rows=max_rows)
    yield {"event": "result", "data": {"sql": sql, **result}}
    yield {"event": "done", "data": {}}


async def ask_agent(question: str, routing: dict | None = None, max_rows: int = 100) -> dict:
    """Translate a question to SQL, execute it read-only, return results.

    ``routing`` is the signed-in user's LLM config (upstream_url / api key /
    auth type / default model). When omitted, the gateway-wide UPSTREAM_*
    env config is used (legacy fallback).
    """
    if not question or not question.strip():
        raise ValueError("Question is empty")
    content = await _call_upstream(question, routing)
    sql = _extract_sql_from_response(content)
    if sql is None:
        raise ValueError("Could not extract SQL from the model response")
    result = execute_readonly_query(sql, max_rows=max_rows)
    return {"sql": sql, **result}
