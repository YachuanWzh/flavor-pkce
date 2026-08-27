"""Per-user quota/budget enforcement for the gateway (gateway.quota).

Three limits, each 0 = disabled:
- RATE_LIMIT_RPM: requests per user per fixed one-minute window.
- DAILY_TOKEN_BUDGET: prompt+completion tokens per UTC day.
- DAILY_COST_BUDGET_USD: estimated USD spend per UTC day (MODEL_PRICES).

Usage is persisted in the audit SQLite (quota_usage table) so budgets
survive restarts; the one-minute window lives in the same row.
"""

import os
import sqlite3
import tempfile
import time

import pytest

import gateway.config as config
from gateway.database import init_audit_db
from gateway.quota import check, get_day_usage, record_usage


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    fd, tmp = tempfile.mkstemp(suffix=".db", prefix="quota_test_")
    os.close(fd)
    old = config.AUDIT_DB_PATH
    config.AUDIT_DB_PATH = tmp
    init_audit_db()
    # Defaults: all limits disabled unless a test enables them.
    monkeypatch.setattr(config, "RATE_LIMIT_RPM", 0, raising=False)
    monkeypatch.setattr(config, "DAILY_TOKEN_BUDGET", 0, raising=False)
    monkeypatch.setattr(config, "DAILY_COST_BUDGET_USD", 0.0, raising=False)
    monkeypatch.setattr(config, "MODEL_PRICES", {}, raising=False)
    yield
    config.AUDIT_DB_PATH = old
    if os.path.exists(tmp):
        os.remove(tmp)


# ---------------------------------------------------------------------------
# Rate limiting (fixed one-minute window)
# ---------------------------------------------------------------------------

def test_check_allows_unlimited_when_rate_limit_disabled():
    for _ in range(100):
        ok, err = check("alice")
        assert ok, err


def test_rate_limit_blocks_after_rpm_within_same_minute():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "RATE_LIMIT_RPM", 3, raising=False)
        now = 1_700_000_000.0  # fixed clock inside one minute bucket
        for i in range(3):
            ok, err = check("alice", now=now)
            assert ok, f"attempt {i} should pass: {err}"
        ok, err = check("alice", now=now + 5)
        assert not ok
        assert err["error"] == "rate_limited"
        assert err["retry_after_seconds"] >= 1


def test_rate_limit_resets_in_next_minute_window():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "RATE_LIMIT_RPM", 2, raising=False)
        now = 1_700_000_000.0
        window_start = int(now // 60) * 60
        assert check("alice", now=now)[0]
        assert check("alice", now=now + 1)[0]
        assert not check("alice", now=window_start + 59)[0]
        # Next minute bucket: allowed again.
        assert check("alice", now=window_start + 60)[0]


def test_rate_limit_is_per_user():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "RATE_LIMIT_RPM", 1, raising=False)
        assert check("alice", now=1_700_000_000.0)[0]
        assert not check("alice", now=1_700_000_000.0)[0]
        assert check("bob", now=1_700_000_000.0)[0]


# ---------------------------------------------------------------------------
# Daily token budget
# ---------------------------------------------------------------------------

def test_token_budget_blocks_when_exhausted():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "DAILY_TOKEN_BUDGET", 1000, raising=False)
        record_usage("alice", prompt_tokens=600, completion_tokens=400)
        ok, err = check("alice")
        assert not ok
        assert err["error"] == "token_budget_exceeded"
        assert err["used"] == 1000
        assert err["budget"] == 1000


def test_token_budget_below_limit_passes():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "DAILY_TOKEN_BUDGET", 1000, raising=False)
        record_usage("alice", prompt_tokens=100, completion_tokens=50)
        assert check("alice")[0]


def test_record_usage_accumulates():
    record_usage("alice", prompt_tokens=100, completion_tokens=20)
    record_usage("alice", prompt_tokens=50, completion_tokens=10)
    usage = get_day_usage("alice")
    assert usage["prompt_tokens"] == 150
    assert usage["completion_tokens"] == 30


def test_record_usage_zero_or_none_tokens_is_noop():
    record_usage("alice", prompt_tokens=None, completion_tokens=None)
    record_usage("alice", prompt_tokens=0, completion_tokens=0)
    usage = get_day_usage("alice")
    assert usage["prompt_tokens"] == 0


# ---------------------------------------------------------------------------
# Daily cost budget (USD, via MODEL_PRICES)
# ---------------------------------------------------------------------------

def test_cost_accumulates_from_model_prices():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "MODEL_PRICES", {
            "gpt-x": {"prompt": 2.0, "completion": 4.0},
        }, raising=False)
        # 1M prompt @ $2 + 500k completion @ $4/M = 2 + 2 = $4
        record_usage("alice", model="gpt-x",
                     prompt_tokens=1_000_000, completion_tokens=500_000)
        usage = get_day_usage("alice")
        assert usage["cost"] == pytest.approx(4.0)


def test_cost_budget_blocks_when_exhausted():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "MODEL_PRICES", {
            "gpt-x": {"prompt": 2.0, "completion": 4.0},
        }, raising=False)
        mp.setattr(config, "DAILY_COST_BUDGET_USD", 10.0, raising=False)
        record_usage("alice", model="gpt-x",
                     prompt_tokens=5_000_000, completion_tokens=0)  # $10
        ok, err = check("alice")
        assert not ok
        assert err["error"] == "cost_budget_exceeded"
        assert err["used"] == pytest.approx(10.0)
        assert err["budget"] == pytest.approx(10.0)


def test_unknown_model_costs_zero():
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "DAILY_COST_BUDGET_USD", 1.0, raising=False)
        record_usage("alice", model="unpriced",
                     prompt_tokens=10_000_000, completion_tokens=0)
        assert check("alice")[0]


# ---------------------------------------------------------------------------
# Day bucketing (UTC)
# ---------------------------------------------------------------------------

def test_usage_is_bucketed_per_utc_day():
    record_usage("alice", prompt_tokens=10, completion_tokens=5,
                 day="2026-08-26")
    record_usage("alice", prompt_tokens=7, completion_tokens=3,
                 day="2026-08-27")
    assert get_day_usage("alice", day="2026-08-26")["prompt_tokens"] == 10
    today = get_day_usage("alice", day="2026-08-27")
    assert today["prompt_tokens"] == 7
    assert today["completion_tokens"] == 3
