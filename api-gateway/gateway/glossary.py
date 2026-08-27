"""Admin-maintained column glossary (semantic annotations) for the data agent.

The schema is fixed (audit_logs / v_audit_agent / v_audit_daily), but the
*business* meaning of a column is not: an admin can annotate e.g.
``status`` as "HTTP 状态码: 200=成功, 4xx=客户端错误, 5xx=服务端错误" or
``level`` as "日志级别". Annotations are rendered into the agent system
prompt so the LLM resolves column semantics consistently across questions
— the lightweight analogue of DataAgent's semantic model.
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


def list_glossary_entries(*, enabled_only: bool = False) -> list[dict]:
    """Return all glossary entries ordered by table/column."""
    conn = _connect()
    try:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = conn.execute(
            f"SELECT * FROM column_glossary {where} "
            "ORDER BY table_name, column_name"
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def upsert_glossary_entry(
    table_name: str,
    column_name: str,
    business_name: str = "",
    synonyms: list[str] | None = None,
    description: str = "",
    *,
    enabled: bool = True,
) -> dict:
    """Insert or update an entry by (table_name, column_name)."""
    table_name = table_name.strip().lower()
    column_name = column_name.strip().lower()
    if not table_name or not column_name:
        raise ValueError("table_name and column_name are required")
    syn_json = json.dumps(synonyms or [], ensure_ascii=False)
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO column_glossary
                   (table_name, column_name, business_name, synonyms,
                    description, enabled, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(table_name, column_name) DO UPDATE SET
                   business_name = excluded.business_name,
                   synonyms = excluded.synonyms,
                   description = excluded.description,
                   enabled = excluded.enabled,
                   updated_at = excluded.updated_at""",
            (table_name, column_name, business_name.strip(),
             syn_json, description.strip(), 1 if enabled else 0, _now()),
        )
        row = conn.execute(
            "SELECT * FROM column_glossary WHERE table_name = ? AND column_name = ?",
            (table_name, column_name),
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_glossary_entry(entry_id: int) -> bool:
    """Delete a glossary entry by id. Returns True when a row was removed."""
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM column_glossary WHERE id = ?", (entry_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def render_glossary_prompt() -> str:
    """Render enabled entries as a system-prompt section (empty when none)."""
    rows = list_glossary_entries(enabled_only=True)
    if not rows:
        return ""
    lines = [
        "Column glossary (business meanings — resolve column semantics using these):"
    ]
    for row in rows:
        parts: list[str] = []
        if row["business_name"]:
            parts.append(f"business name: {row['business_name']}")
        try:
            syns = json.loads(row["synonyms"])
        except (json.JSONDecodeError, TypeError):
            syns = []
        if syns:
            parts.append(f"synonyms: {'、'.join(syns)}")
        if row["description"]:
            parts.append(row["description"])
        suffix = f" ({'; '.join(parts)})" if parts else ""
        lines.append(f"- {row['table_name']}.{row['column_name']}{suffix}")
    return "\n".join(lines)
