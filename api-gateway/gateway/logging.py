"""Structured JSON logging for the API Gateway.

Every request is logged as a single JSON line to stdout, suitable for
forwarding to ELK, Loki, or other log aggregation systems.
"""

import json
import logging
import sys
from datetime import datetime, timezone

_logger = logging.getLogger("gateway")


def setup_logging() -> None:
    """Configure the gateway logger with JSON-on-stdout output.

    Called once at application startup.  The handler writes plain-text
    JSON lines (no ANSI, no timestamps embedded by the formatter) so
    downstream collectors can parse them directly.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def log_request(
    *,
    user: str = "-",
    method: str = "-",
    path: str = "-",
    status: int = 0,
    duration_ms: float = 0.0,
    upstream_ms: float | None = None,
    level: str = "INFO",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    model: str | None = None,
    session_id: str | None = None,
    request_body: str | None = None,
    response_body: str | None = None,
) -> None:
    """Emit a single JSON log line for a request."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "user": user,
        "method": method,
        "path": path,
        "status": status,
        "duration_ms": round(duration_ms, 2),
    }
    if upstream_ms is not None:
        entry["upstream_ms"] = round(upstream_ms, 2)
    if prompt_tokens is not None:
        entry["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        entry["completion_tokens"] = completion_tokens
    if model is not None:
        entry["model"] = model
    if session_id is not None:
        entry["session_id"] = session_id

    getattr(_logger, level.lower())(json.dumps(entry, default=str))

    _persist_to_db(
        timestamp=entry["timestamp"],
        user=user,
        method=method,
        path=path,
        status=status,
        duration_ms=round(duration_ms, 2),
        upstream_ms=round(upstream_ms, 2) if upstream_ms is not None else None,
        level=level,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=model,
        session_id=session_id,
        request_body=request_body,
        response_body=response_body,
    )


def _persist_to_db(**kwargs: object) -> None:
    """Persist a log entry to the audit database (best-effort).

    Failures (e.g. disk full) are swallowed — they must never break the
    request path.
    """
    try:
        from gateway.database import insert_log
        insert_log(**kwargs)  # type: ignore[arg-type]
    except Exception:
        pass
