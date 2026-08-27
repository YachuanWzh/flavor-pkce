"""Audit retention purge + configurable body truncation (gateway.retention).

Mirrors auth-server's AUDIT_RETENTION_DAYS policy: gateway audit rows older
than the cutoff (audit_logs, agent_queries, quota_usage day rows) are
deleted on startup — LLM request/response bodies are user prompt text, so
unbounded growth is both a disk and a privacy risk.

The per-row body limit for *new* rows is configurable via
``AUDIT_BODY_MAX_CHARS`` (0 = store no bodies at all).  Both live in
:func:`truncate_body`, which ``database.insert_log`` delegates to.
"""

import sqlite3

import gateway.config
from gateway.database import _connect


def truncate_body(text: str | None) -> str | None:
    """Truncate body text to ``config.AUDIT_BODY_MAX_CHARS`` with a marker.

    A limit of 0 (or below) stores no body at all — privacy / low-disk mode.
    """
    if text is None:
        return None
    limit = gateway.config.AUDIT_BODY_MAX_CHARS
    if limit <= 0:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated]"


def purge_old_logs() -> int:
    """Delete audit rows older than AUDIT_RETENTION_DAYS. Returns rows deleted.

    0 disables retention (returns 0). The hash chain stays valid afterwards:
    ``verify_integrity`` walks whatever rows remain from the first surviving
    row forward.
    """
    from datetime import datetime, timedelta, timezone

    days = gateway.config.AUDIT_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        deleted = conn.execute(
            "DELETE FROM audit_logs WHERE timestamp < ?", (cutoff,)
        ).rowcount
        # agent_queries uses the same ISO timestamp format.
        conn.execute("DELETE FROM agent_queries WHERE timestamp < ?", (cutoff,))
        # quota_usage is keyed by UTC day string.
        conn.execute("DELETE FROM quota_usage WHERE day < ?", (cutoff[:10],))
        conn.commit()
        return max(0, deleted)
    except sqlite3.Error:
        conn.rollback()
        return 0
    finally:
        conn.close()
