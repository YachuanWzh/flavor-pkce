"""Admin-maintained preset questions for the data-agent chat.

Preset questions are one-click shortcuts shown above the chat input so
administrators can ask their frequent questions (e.g. "最近 7 天请求量
趋势") without typing. The chat UI only reads enabled presets; the admin
API manages the full list including enable/disable and sort order.
"""

from datetime import datetime, timezone

from gateway.database import _connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    data = dict(row)
    data["enabled"] = bool(data.get("enabled"))
    return data


def list_preset_questions(*, enabled_only: bool = False) -> list[dict]:
    """Return preset questions ordered by sort_order, then id."""
    conn = _connect()
    try:
        where = "WHERE enabled = 1" if enabled_only else ""
        rows = conn.execute(
            f"SELECT * FROM preset_questions {where} "
            "ORDER BY sort_order ASC, id ASC"
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def upsert_preset_question(
    question: str,
    *,
    enabled: bool = True,
    sort_order: int = 0,
) -> dict:
    """Insert or update a preset question by unique text."""
    question = question.strip()
    if not question:
        raise ValueError("question is required")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO preset_questions (question, enabled, sort_order, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(question) DO UPDATE SET
                   enabled = excluded.enabled,
                   sort_order = excluded.sort_order,
                   updated_at = excluded.updated_at""",
            (question, 1 if enabled else 0, int(sort_order), _now()),
        )
        row = conn.execute(
            "SELECT * FROM preset_questions WHERE question = ?", (question,)
        ).fetchone()
        conn.commit()
        return _row_to_dict(row)
    finally:
        conn.close()


def delete_preset_question(preset_id: int) -> bool:
    """Delete a preset question by id. Returns True when a row was removed."""
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM preset_questions WHERE id = ?", (preset_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
