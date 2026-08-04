"""Auth-server audit event logging (P0-10).

Every security-relevant event (register, login success/failure, token
exchange, refresh, revoke) is appended to ``audit_logs`` with the actor,
source IP and user-agent so internal auditors can reconstruct activity.
"""

import json
from datetime import datetime, timezone, timedelta

from auth_server.database import get_db
import auth_server.config as server_config


def _client_ip(request) -> str:
    if request is None or request.client is None:
        return ""
    return request.client.host


def log_event(
    request,
    event: str,
    *,
    actor_user_id: str | None = None,
    actor_username: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append one audit event row. Never raises on write failure."""
    try:
        user_agent = ""
        if request is not None:
            user_agent = request.headers.get("user-agent", "")[:512]
        db = get_db()
        db.execute(
            """INSERT INTO audit_logs
                   (event, actor_user_id, actor_username, ip, user_agent, detail)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event,
                actor_user_id,
                actor_username,
                _client_ip(request),
                user_agent,
                json.dumps(detail or {}, ensure_ascii=False)[:8000],
            ),
        )
        db.commit()
        db.close()
    except Exception:
        # Audit must never take the auth flow down.
        pass


def purge_old_audit_logs() -> int:
    """Delete audit rows older than AUDIT_RETENTION_DAYS. Returns rows deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=server_config.AUDIT_RETENTION_DAYS)
    db = get_db()
    cur = db.execute(
        "DELETE FROM audit_logs WHERE created_at < ?",
        (cutoff.isoformat(),),
    )
    db.commit()
    deleted = cur.rowcount
    db.close()
    return deleted
