"""Anomaly detection: daily scan over audit aggregates → alerts table + API.

Rules compare yesterday against the trailing 7-day baseline from the
audit_logs daily aggregate (volume, error rate, latency). Notification
(webhook/email) is intentionally a TODO — alerts are persisted and shown
on the dashboard for now.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import gateway.config as config
from gateway.database import init_audit_db, insert_log


@pytest.fixture(autouse=True)
def setup_db():
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="anomaly_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    old_token = config.AUDIT_API_TOKEN
    config.AUDIT_DB_PATH = tmp
    config.AUDIT_API_TOKEN = "test-audit-token"
    init_audit_db()
    yield
    config.AUDIT_DB_PATH = old
    config.AUDIT_API_TOKEN = old_token
    if os.path.exists(tmp):
        os.remove(tmp)


@pytest.fixture(autouse=True)
def setup_keys():
    keys_dir = os.path.join(os.path.dirname(__file__), "..", "..", "auth-server", "keys")
    os.makedirs(keys_dir, exist_ok=True)
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "auth-server"))
    from auth_server.jwt_utils import _ensure_keys_exist
    _ensure_keys_exist()
    config.JWT_PUBLIC_KEY_PATH = os.path.join(keys_dir, "public.pem")
    yield


@pytest.fixture
def client():
    from gateway.main import app
    return TestClient(app)


AUTH = {"X-Audit-Token": "test-audit-token"}

# Scans evaluate the day before "today"; anchor the fixtures to the real
# current date so API-triggered scans (which use the default clock) agree
# with directly-called scan_daily(today=BASE_DAY).
BASE_DAY = datetime.now(timezone.utc)


def _fill_day(days_ago: int, requests: int, errors: int = 0, avg_ms: float = 100.0):
    """Bulk-insert rows shaping the daily aggregate (no hash chain needed —
    these tests only read aggregates, never verify integrity)."""
    day = (BASE_DAY - timedelta(days=days_ago)).replace(hour=12).isoformat()
    rows = []
    for i in range(requests):
        status = 502 if i < errors else 200
        level = "ERROR" if i < errors else "INFO"
        rows.append((day, "alice", "POST", "/v1/chat", status, avg_ms, level))
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    conn.executemany(
        "INSERT INTO audit_logs (timestamp, \"user\", method, path, status,"
        " duration_ms, level) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _normal_week():
    """7 baseline days (2..8 days ago): 100 req/day, 2 errors, 100ms."""
    for d in range(2, 9):
        _fill_day(d, requests=100, errors=2, avg_ms=100.0)


def _alerts():
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    rows = conn.execute(
        "SELECT day, kind, message FROM gateway_alerts ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def _scan(today=None):
    from gateway.anomaly import scan_daily
    return scan_daily(today=today or BASE_DAY)


# ---------------------------------------------------------------------------
# Scan rules
# ---------------------------------------------------------------------------

def test_scan_no_alerts_for_normal_day():
    _normal_week()
    _fill_day(1, requests=100, errors=2, avg_ms=100.0)  # yesterday normal
    alerts = _scan()
    assert alerts == []


def test_scan_detects_error_rate_spike():
    _normal_week()                       # ~2% error rate
    _fill_day(1, requests=100, errors=30)  # 30% yesterday
    kinds = [a["kind"] for a in _scan()]
    assert "error_rate_spike" in kinds


def test_scan_detects_volume_spike_and_drop():
    _normal_week()                       # 100/day baseline
    _fill_day(1, requests=400, errors=2)   # 4x
    kinds = [a["kind"] for a in _scan()]
    assert "volume_spike" in kinds


def test_scan_detects_latency_spike():
    _normal_week()                       # ~100ms baseline
    _fill_day(1, requests=100, errors=2, avg_ms=5000.0)
    kinds = [a["kind"] for a in _scan()]
    assert "latency_spike" in kinds


def test_scan_skips_low_traffic_days():
    """Tiny samples must not fire — noise would bury real alerts."""
    _normal_week()
    _fill_day(1, requests=3, errors=2)   # 66% errors but n=3 < min volume
    alerts = _scan()
    assert [a for a in alerts if a["kind"] == "error_rate_spike"] == []


def test_scan_ignores_today_partial_data():
    """Only the completed day before the scan day is evaluated."""
    _normal_week()
    _fill_day(1, requests=400, errors=2)  # yesterday: spike → alert
    # Today (BASE_DAY itself) also spikes, but must not double-fire.
    _fill_day(0, requests=900, errors=2)
    alerts = _scan()
    expected_day = (BASE_DAY - timedelta(days=1)).strftime("%Y-%m-%d")
    days = {a["day"] for a in alerts}
    assert days == {expected_day}


def test_scan_is_idempotent_per_day():
    _normal_week()
    _fill_day(1, requests=400, errors=2)
    _scan()
    _scan()  # second run must not duplicate rows
    conn = sqlite3.connect(config.AUDIT_DB_PATH)
    rows = conn.execute(
        "SELECT day, kind, COUNT(*) c FROM gateway_alerts "
        "GROUP BY day, kind HAVING c > 1"
    ).fetchall()
    conn.close()
    assert rows == []
    assert len(_alerts()) >= 1


def test_scan_detects_volume_drop():
    _normal_week()                       # 100/day baseline
    _fill_day(1, requests=20, errors=0)  # 5x drop
    kinds = [a["kind"] for a in _scan()]
    assert "volume_drop" in kinds


def test_scan_without_baseline_does_not_crash():
    """First deployment: yesterday exists but no history yet — no alerts."""
    _fill_day(1, requests=500, errors=100)
    assert _scan() == []


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------

def test_alerts_requires_auth(client):
    assert client.get("/api/alerts").status_code == 401
    assert client.post("/api/alerts/1/ack", headers={}).status_code == 401


def test_scan_endpoint_triggers_scan_and_returns_alerts(client):
    _normal_week()
    _fill_day(1, requests=400, errors=2)
    resp = client.post("/api/alerts/scan", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert any(a["kind"] == "volume_spike" for a in body["alerts"])


def test_alerts_list_and_ack_flow(client):
    _normal_week()
    _fill_day(1, requests=400, errors=2)
    client.post("/api/alerts/scan", headers=AUTH)

    resp = client.get("/api/alerts", headers=AUTH)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["acked"] == 0
    alert_id = items[0]["id"]

    resp = client.post(f"/api/alerts/{alert_id}/ack", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["acked"] is True

    # Default list shows only open alerts.
    assert client.get("/api/alerts", headers=AUTH).json()["items"] == []
    # include_acked reveals it.
    all_items = client.get(
        "/api/alerts?include_acked=true", headers=AUTH,
    ).json()["items"]
    assert [a["id"] for a in all_items] == [alert_id]


def test_ack_unknown_alert_404(client):
    resp = client.post("/api/alerts/9999/ack", headers=AUTH)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Startup wiring: booting the gateway runs one scan (scheduler task)
# ---------------------------------------------------------------------------

def test_startup_runs_a_scan():
    """Entering the app lifespan must evaluate the completed day so the
    dashboard shows fresh alerts without anyone clicking scan."""
    import gateway.main as gm
    from fastapi.testclient import TestClient

    _normal_week()
    _fill_day(1, requests=400, errors=2)
    # The `with` block fires lifespan startup (plain TestClient does not).
    with TestClient(gm.app) as c:
        resp = c.get("/api/alerts", headers=AUTH)
    assert resp.status_code == 200
    assert any(a["kind"] == "volume_spike" for a in resp.json()["items"])
