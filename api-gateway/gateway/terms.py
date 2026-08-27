"""Admin-maintained metric/business-term dictionary for the data agent.

Terms are injected into the agent system prompt (agent_loop.py / agent.py)
so the LLM resolves business jargon consistently (e.g. "命中率" ->
cache_read / (prompt + cache_read + cache_creation)). Synonyms are stored
as a JSON string array; only enabled terms are rendered into prompts.
"""

import json
from datetime import datetime, timezone

from gateway.database import _connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    return data


def list_metric_terms(*, enabled_only: bool = False) -> list[dict]:
    """Return all metric terms, newest first, as plain dicts."""
    conn = _connect()
    try:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = conn.execute(
            f"SELECT * FROM metric_terms {where} ORDER BY id DESC"
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def upsert_metric_term(
    term: str,
    definition: str,
    synonyms: list[str] | None = None,
    *,
    enabled: bool = True,
) -> dict:
    """Insert or update a term by unique key. Returns the stored row."""
    term = term.strip()
    definition = definition.strip()
    if not term or not definition:
        raise ValueError("term and definition are required")
    syn_json = json.dumps(synonyms or [], ensure_ascii=False)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO metric_terms (term, definition, synonyms, enabled, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(term) DO UPDATE SET
                   definition = excluded.definition,
                   synonyms = excluded.synonyms,
                   enabled = excluded.enabled,
                   updated_at = excluded.updated_at""",
            (term, definition, syn_json, 1 if enabled else 0, _now()),
        )
        row = conn.execute(
            "SELECT * FROM metric_terms WHERE term = ?", (term,)
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_metric_term(term_id: int) -> bool:
    """Delete a term by id. Returns True when a row was removed."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM metric_terms WHERE id = ?", (term_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def render_metric_prompt() -> str:
    """Render enabled terms as a system-prompt section (empty when none)."""
    rows = list_metric_terms(enabled_only=True)
    if not rows:
        return ""
    lines = ["Metric dictionary (business definitions — resolve questions using these):"]
    for row in rows:
        try:
            syns = json.loads(row["synonyms"])
        except (json.JSONDecodeError, TypeError):
            syns = []
        suffix = f" (synonyms: {'、'.join(syns)})" if syns else ""
        lines.append(f"- {row['term']}{suffix}: {row['definition']}")
    return "\n".join(lines)
