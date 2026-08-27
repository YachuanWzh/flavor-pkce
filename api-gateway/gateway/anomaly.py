"""Daily anomaly detection over the gateway audit aggregates (gateway.anomaly).

One scan evaluates the most recently completed UTC day against the
trailing 7-day baseline from ``audit_logs`` daily aggregates and persists
findings to ``gateway_alerts`` (UNIQUE per day+kind → idempotent reruns):

- ``volume_spike`` / ``volume_drop``   — request count ≥3x / ≤0.25x baseline
- ``error_rate_spike``                 — error ratio ≥3x baseline ratio
- ``latency_spike``                    — avg duration ≥3x baseline

Low-traffic days (below MIN_REQUESTS) are skipped so a couple of failing
calls can't bury real signals.  Thresholds are fixed multipliers — the
single-machine scale doesn't justify statsmodels/STL.

TODO(notification): push new alerts to a webhook/email channel. Until
then, alerts surface via ``GET /api/alerts`` and the dashboard banner.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

from gateway.database import _connect

BASELINE_DAYS = 7          # trailing days compared against
MIN_REQUESTS = 10          # skip low-traffic target days
MIN_BASELINE_REQUESTS = 10 # skip when the baseline itself is noise
SPIKE_FACTOR = 3.0         # generic "how many times over baseline" trigger
DROP_FACTOR = 0.25         # volume below this fraction of baseline
ERROR_RATE_FLOOR = 0.05    # never alert under 5% error rate regardless of ratio


def _utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_series(conn, start_day: str, end_day: str) -> dict[str, dict]:
    """Daily aggregates keyed by day for [start_day, end_day] inclusive."""
    rows = conn.execute(
        """SELECT substr(timestamp, 1, 10) AS day,
                  COUNT(*) AS requests,
                  SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS errors,
                  AVG(duration_ms) AS avg_ms
           FROM audit_logs
           WHERE substr(timestamp, 1, 10) BETWEEN ? AND ?
           GROUP BY day""",
        (start_day, end_day),
    ).fetchall()
    return {row["day"]: dict(row) for row in rows}


def _evaluate(target_day: str, day: dict, baseline: list[dict]) -> list[dict]:
    """Return alert dicts for one completed day against its baseline."""
    requests = day.get("requests", 0) or 0
    if requests < MIN_REQUESTS:
        return []

    base_requests = sum(item["requests"] for item in baseline) / len(baseline)
    if base_requests < MIN_BASELINE_REQUESTS:
        return []  # no trustworthy baseline yet (e.g. first week)
    base_errors = sum(item["errors"] for item in baseline)
    base_total = sum(item["requests"] for item in baseline)
    base_error_rate = (base_errors / base_total) if base_total else 0.0
    base_ms = sum(item["avg_ms"] for item in baseline) / len(baseline)

    alerts: list[dict] = []

    def alert(kind: str, message: str, metric: float, reference: float):
        alerts.append({
            "day": target_day, "kind": kind, "message": message,
            "metric": round(metric, 4), "baseline": round(reference, 4),
        })

    if requests >= base_requests * SPIKE_FACTOR:
        alert("volume_spike",
              f"Requests {requests} ≥ {SPIKE_FACTOR}x daily baseline "
              f"({base_requests:.0f})",
              requests, base_requests)
    elif requests <= base_requests * DROP_FACTOR:
        alert("volume_drop",
              f"Requests {requests} ≤ {DROP_FACTOR:.0%} of daily baseline "
              f"({base_requests:.0f})",
              requests, base_requests)

    error_rate = (day.get("errors", 0) or 0) / requests if requests else 0.0
    if (error_rate >= ERROR_RATE_FLOOR
            and error_rate >= max(base_error_rate * SPIKE_FACTOR, ERROR_RATE_FLOOR)
            and error_rate > base_error_rate):
        alert("error_rate_spike",
              f"Error rate {error_rate:.1%} vs baseline {base_error_rate:.1%}",
              error_rate, base_error_rate)

    avg_ms = day.get("avg_ms") or 0.0
    if base_ms > 0 and avg_ms >= base_ms * SPIKE_FACTOR:
        alert("latency_spike",
              f"Avg latency {avg_ms:.0f}ms ≥ {SPIKE_FACTOR}x baseline "
              f"({base_ms:.0f}ms)",
              avg_ms, base_ms)

    return alerts


def scan_daily(today: datetime | None = None) -> list[dict]:
    """Evaluate the most recently completed UTC day; persist new alerts.

    ``today`` is injectable for tests. Reruns for the same day are
    idempotent (INSERT OR IGNORE on UNIQUE(day, kind)). Returns the alerts
    for the scanned day (freshly raised or already stored).
    """
    today = today or datetime.now(timezone.utc)
    target_day = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    baseline_start = (today - timedelta(days=1 + BASELINE_DAYS)).strftime("%Y-%m-%d")
    baseline_end = (today - timedelta(days=2)).strftime("%Y-%m-%d")

    conn = _connect()
    try:
        series = _day_series(conn, baseline_start, target_day)
        target = series.get(target_day)
        baseline = [
            series[day] for day in sorted(series)
            if baseline_start <= day <= baseline_end
        ]
        if target is None or not baseline:
            return []  # empty day or first deployment: nothing to compare
        findings = _evaluate(target_day, target, baseline)
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in findings:
            conn.execute(
                """INSERT OR IGNORE INTO gateway_alerts
                   (day, kind, message, metric, baseline, acked, created_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (item["day"], item["kind"], item["message"],
                 item["metric"], item["baseline"], now_iso),
            )
        # TODO(notification): hand new findings to a notifier (webhook/email)
        #   once a channel is configured. notify_alerts(findings) lives here.
        conn.commit()
        rows = conn.execute(
            "SELECT id, day, kind, message, metric, baseline, acked "
            "FROM gateway_alerts WHERE day = ? ORDER BY id",
            (target_day,),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        conn.rollback()
        return []
    finally:
        conn.close()


def list_alerts(include_acked: bool = False) -> list[dict]:
    """Open (unacknowledged) alerts, newest day first.

    ``include_acked=True`` returns everything (the list page's history tab).
    """
    conn = _connect()
    try:
        where = "" if include_acked else "WHERE acked = 0"
        rows = conn.execute(
            f"SELECT id, day, kind, message, metric, baseline, acked, created_at "
            f"FROM gateway_alerts {where} ORDER BY day DESC, id DESC LIMIT 200"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def ack_alert(alert_id: int) -> bool:
    """Acknowledge one alert. Returns False when the id is unknown."""
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE gateway_alerts SET acked = 1 WHERE id = ?", (alert_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
