"""Agent conversation session store tests (agent-loop Task 3)."""

import time

import pytest

from gateway.session import SessionStore, SessionState


@pytest.fixture
def store():
    return SessionStore(ttl_seconds=600, max_sessions=8)


def test_create_and_get(store):
    session = store.create(user_id="alice")
    assert session.session_id
    assert store.get(session.session_id) is session


def test_get_missing_returns_none(store):
    assert store.get("nope") is None


def test_messages_default_empty(store):
    session = store.create()
    assert session.messages == []
    assert session.summary is None


def test_expired_session_purged():
    store = SessionStore(ttl_seconds=0)
    session = store.create()
    time.sleep(0.01)
    assert store.get(session.session_id) is None


def test_capacity_evicts_oldest():
    store = SessionStore(ttl_seconds=600, max_sessions=2)
    first = store.create(user_id="u1")
    store.create(user_id="u2")
    third = store.create(user_id="u3")
    # The oldest session is evicted to keep capacity bounded.
    assert store.get(first.session_id) is None
    assert store.get(third.session_id) is not None


def test_touch_refreshes_last_active(store):
    store = SessionStore(ttl_seconds=100)
    session = store.create()
    before = session.last_active_at
    time.sleep(0.01)
    store.touch(session.session_id)
    assert store.get(session.session_id).last_active_at > before


def test_session_ids_unique(store):
    ids = {store.create().session_id for _ in range(50)}
    assert len(ids) == 50


def test_session_state_records_sql_attempts():
    state = SessionState("sid")
    state.pending_sql = "SELECT 1"
    state.pending_attempt = 1
    assert state.pending_sql == "SELECT 1"
    assert state.pending_attempt == 1
