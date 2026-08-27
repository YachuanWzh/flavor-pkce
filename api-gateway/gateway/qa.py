"""Admin-maintained Q&A knowledge pairs for the data agent.

Few-shot correction pairs: when a class of questions keeps producing
wrong SQL, an admin stores the canonical question → SQL approach here
instead of rewriting the prompt. Before each generation the agent matches
the current user question against enabled pairs (token/bigram overlap
plus substring boost) and injects the top hits into the system prompt, so
the LLM follows the curated SQL pattern for those questions.

This is the lightweight analogue of DataAgent's agent-knowledge Q&A
recall — no vector store, just deterministic keyword matching over an
admin-curated, bounded table.
"""

import json
import re
from datetime import datetime, timezone

from gateway.database import _connect

# Max pairs injected into one system prompt.
MAX_QA_PROMPT_HITS = 3

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    return data


def list_qa_pairs(*, enabled_only: bool = False) -> list[dict]:
    """Return all QA pairs, newest first, as plain dicts."""
    conn = _connect()
    try:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = conn.execute(
            f"SELECT * FROM agent_qa_pairs {where} ORDER BY id DESC"
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def upsert_qa_pair(
    question: str,
    sql_template: str,
    tags: list[str] | None = None,
    *,
    enabled: bool = True,
) -> dict:
    """Insert or update a QA pair by unique question. Returns the stored row."""
    question = question.strip()
    sql_template = sql_template.strip()
    if not question or not sql_template:
        raise ValueError("question and sql_template are required")
    tags_json = json.dumps(tags or [], ensure_ascii=False)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO agent_qa_pairs (question, sql_template, tags, enabled, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(question) DO UPDATE SET
                   sql_template = excluded.sql_template,
                   tags = excluded.tags,
                   enabled = excluded.enabled,
                   updated_at = excluded.updated_at""",
            (question, sql_template, tags_json, 1 if enabled else 0, _now()),
        )
        row = conn.execute(
            "SELECT * FROM agent_qa_pairs WHERE question = ?", (question,)
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_qa_pair(pair_id: int) -> bool:
    """Delete a QA pair by id. Returns True when a row was removed."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM agent_qa_pairs WHERE id = ?", (pair_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _tokens(text: str) -> set[str]:
    """Significant tokens: ASCII words plus sliding CJK character bigrams.

    Bigrams keep the matcher dependency-free while still handling Chinese
    questions ("统计上个月的销售额" → 上个 / 个月 / 月销 / …). No tokeniser
    is required, and matches stay deterministic and testable.
    """
    text = (text or "").lower()
    tokens = set(_ASCII_TOKEN_RE.findall(text))
    cjk = _CJK_RE.findall(text)
    tokens.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return tokens


def match_qa_pairs(question: str, limit: int = MAX_QA_PROMPT_HITS) -> list[dict]:
    """Rank enabled QA pairs by token overlap with ``question``.

    A pair is returned only when it shares at least one significant token
    with the question; substring containment (either direction) adds a
    strong boost. Returns at most ``limit`` pairs, best matches first.
    """
    q_text = (question or "").strip()
    if not q_text:
        return []
    q_tokens = _tokens(q_text)
    if not q_tokens:
        return []
    q_lower = q_text.lower()

    scored: list[tuple[int, dict]] = []
    for pair in list_qa_pairs(enabled_only=True):
        qa_q = (pair.get("question") or "").strip()
        if not qa_q:
            continue
        qa_lower = qa_q.lower()
        score = len(q_tokens & _tokens(qa_q))
        if qa_lower in q_lower or q_lower in qa_lower:
            score += 5
        if score > 0:
            scored.append((score, pair))

    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [pair for _, pair in scored[:limit]]


def render_qa_prompt(question: str | None) -> str:
    """Render matched QA pairs as few-shot examples (empty when none match).

    Called from the agent system-prompt builders; ``question`` is the
    current user question so only relevant examples are injected.
    """
    hits = match_qa_pairs(question) if question else []
    if not hits:
        return ""
    lines = [
        "Few-shot examples (when the user question matches one of these, "
        "follow the shown SQL approach):"
    ]
    for pair in hits:
        lines.append(f"Q: {pair['question']}")
        lines.append(f"SQL: {pair['sql_template']}")
    return "\n".join(lines)
