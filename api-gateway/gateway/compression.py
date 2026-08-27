"""Two-level context compression for the agent conversation window.

Strategy A (turn compression): keep the last ``RECENT_TURNS_KEPT`` turns
verbatim and compress every older turn (question + generated SQL + tool
result) into a single compact record. Applied repeatedly, so a second
pass folds already-compressed records together again.

Strategy B (whole-context LLM summary): when strategy A can no longer
bring the context under the threshold, an upstream LLM summarises the
older turns into the required slots — 用户需求 / 已执行步骤 / 未执行步骤 /
用户偏好 — and the summary replaces them.
"""

import os

import httpx

import gateway.config

RECENT_TURNS_KEPT = 5
_TURN_FIELD_LIMIT = 200
SUMMARY_TOKEN_THRESHOLD = int(os.environ.get("AGENT_CONTEXT_TOKENS", "4000"))

SUMMARY_SYSTEM_PROMPT = """You are a context-compression assistant. Summarise the
conversation history below into concise Chinese notes with EXACTLY these sections:
- 用户需求: what the user is trying to find out
- 已执行步骤: queries already executed and their key results
- 未执行步骤: planned or pending steps
- 用户偏好: any stated preferences or constraints
Be factual and compact. Do not invent facts not present in the history."""


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) used only for thresholds."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimated tokens for a message list (content + per-message overhead)."""
    return sum(estimate_tokens(m.get("content") or "") + 4 for m in messages)


def _group_turns(messages: list[dict]) -> list[list[dict]]:
    """Split messages into turns; each turn starts at a 'user' message."""
    turns: list[list[dict]] = []
    current: list[dict] = []
    for message in messages:
        if message.get("role") == "user" and current:
            turns.append(current)
            current = []
        current.append(message)
    if current:
        turns.append(current)
    return turns


def _compress_turn(turn: list[dict]) -> dict:
    """Fold one turn into a single compact record.

    Messages already compressed by an earlier pass are carried through so
    repeated strategy-A rounds keep their information.
    """
    parts: list[str] = []
    for message in turn:
        role = message.get("role")
        content = (message.get("content") or "")[:_TURN_FIELD_LIMIT]
        if role == "compressed":
            parts.append(content)
        elif role == "user":
            parts.append(f"Q: {content}")
        elif role == "assistant":
            parts.append(f"A: {content}")
        elif role == "tool":
            parts.append(f"Tool: {content}")
    return {
        "role": "compressed",
        "content": "[compressed turn] " + " | ".join(parts),
    }


def _flatten(turns: list[list[dict]]) -> list[dict]:
    return [message for turn in turns for message in turn]


def apply_summary(
    messages: list[dict],
    summary: str,
    keep_recent_5_tokens: bool = False,
) -> list[dict]:
    """Build the post-summary message list."""
    summary_message = {"role": "summary", "content": summary}
    if not keep_recent_5_tokens:
        return [summary_message]
    turns = _group_turns(messages)
    recent = _flatten(turns[-RECENT_TURNS_KEPT:]) if turns else []
    return [summary_message] + recent


def compress_context(
    messages: list[dict],
    threshold_tokens: int = SUMMARY_TOKEN_THRESHOLD,
    keep_recent_5_tokens: bool = False,
    summarize_fn=None,
) -> dict:
    """Compress ``messages`` until they fit under ``threshold_tokens``.

    Returns ``{"messages", "strategy", "summary_text_needed"}`` where
    strategy is None / "turn_compress" / "llm_summary". When strategy B is
    required and no ``summarize_fn`` is supplied, the original messages are
    returned unchanged with ``summary_text_needed`` carrying the text an
    async caller should summarise (the agent loop then awaits the LLM).
    """
    if estimate_messages_tokens(messages) <= threshold_tokens:
        return {"messages": messages, "strategy": None, "summary_text_needed": None}

    # Strategy A: one pass — the agent loop calls compress_context again
    # on later turns, so repeated A-compression happens naturally across
    # turns and keeps folding older turns together.
    turns = _group_turns(messages)
    if len(turns) > RECENT_TURNS_KEPT:
        older, recent = turns[:-RECENT_TURNS_KEPT], turns[-RECENT_TURNS_KEPT:]
        current = [_compress_turn(turn) for turn in older] + _flatten(recent)
        if estimate_messages_tokens(current) <= threshold_tokens:
            return {"messages": current, "strategy": "turn_compress", "summary_text_needed": None}
    else:
        current = messages

    # Strategy B: still over the threshold — hand the context to the LLM.
    older_text = "\n".join(m.get("content") or "" for m in current)
    if summarize_fn is None:
        return {
            "messages": messages,
            "strategy": "llm_summary",
            "summary_text_needed": older_text,
        }
    summary = summarize_fn(older_text)
    return {
        "messages": apply_summary(messages, summary, keep_recent_5_tokens),
        "strategy": "llm_summary",
        "summary_text_needed": None,
    }


def _resolve_upstream(routing: dict | None) -> tuple[str, str, str, str]:
    """(base_url, api_key, auth_type, model) from routing or gateway env."""
    if routing:
        return (
            str(routing.get("upstream_url") or gateway.config.UPSTREAM_URL).rstrip("/"),
            str(routing.get("upstream_api_key") or gateway.config.UPSTREAM_API_KEY),
            str(routing.get("upstream_auth_type") or gateway.config.UPSTREAM_AUTH_TYPE),
            str(routing.get("default_model") or gateway.config.UPSTREAM_MODEL),
        )
    return (
        gateway.config.UPSTREAM_URL.rstrip("/"),
        gateway.config.UPSTREAM_API_KEY,
        gateway.config.UPSTREAM_AUTH_TYPE,
        gateway.config.UPSTREAM_MODEL,
    )


async def summarize_via_llm(routing: dict | None, text: str) -> str:
    """Ask the configured upstream LLM for the whole-context summary."""
    base, api_key, auth_type, model = _resolve_upstream(routing)
    headers = {"Content-Type": "application/json"}
    if api_key:
        if auth_type == "bearer":
            headers["Authorization"] = f"Bearer {api_key}"
        elif auth_type == "api-key":
            headers["api-key"] = api_key
        else:
            headers["x-api-key"] = api_key

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": text[:20000]},
        ],
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base}/chat/completions", headers=headers, json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
    return data["choices"][0]["message"]["content"]
