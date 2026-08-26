"""NL-to-SQL data agent for the audit log.

The agent sends the audit schema plus the user's natural-language question
to the gateway's configured upstream LLM (OpenAI-compatible chat/completions),
extracts a single SELECT statement, and executes it through the read-only
query executor. The upstream call reuses the gateway's own configured
credential (UPSTREAM_URL / UPSTREAM_API_KEY), so the agent speaks to the
same providers the gateway trusts.
"""

import re

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


def _extract_sql_from_response(text: str) -> str | None:
    """Pull a single SELECT statement out of a model response."""
    m = re.search(r"```(?:sql)?\s*(SELECT[\s\S]*?)```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(";")
    m = re.search(r"(SELECT[\s\S]*?);?\s*$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip().rstrip(";")
    return None


async def _call_upstream(question: str) -> str:
    """Call the gateway's configured upstream LLM (OpenAI-compatible)."""
    url = f"{gateway.config.UPSTREAM_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": "default",
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
    }
    headers = {"Content-Type": "application/json"}
    if gateway.config.UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {gateway.config.UPSTREAM_API_KEY}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Upstream LLM returned an unexpected response")


async def ask_agent(question: str, max_rows: int = 100) -> dict:
    """Translate a question to SQL, execute it read-only, return results."""
    if not question or not question.strip():
        raise ValueError("Question is empty")
    content = await _call_upstream(question)
    sql = _extract_sql_from_response(content)
    if sql is None:
        raise ValueError("Could not extract SQL from the model response")
    result = execute_readonly_query(sql, max_rows=max_rows)
    return {"sql": sql, **result}
