"""In-memory conversation sessions for the data agent.

The agent loop is stateful: the server keeps the conversation so the
multi-turn history is unbounded on the client side and can be compressed
when the context window fills up. Sessions live in process memory with a
TTL and a hard capacity bound (LRU-style eviction of the oldest entry).
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class SessionState:
    """One data-agent conversation."""

    session_id: str
    user_id: str | None = None
    user: str | None = None
    messages: list[dict] = field(default_factory=list)
    summary: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    # SQL awaiting human confirmation, and how many generation attempts
    # produced it within the current question.
    pending_sql: str | None = None
    pending_question: str | None = None
    pending_attempt: int = 0
    # Serialises concurrent chat/confirm requests on this session so turns
    # cannot interleave at await points or double-execute pending SQL.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SessionStore:
    """Bounded, TTL-expiring session registry."""

    def __init__(self, ttl_seconds: float = 3600.0, max_sessions: int = 1024):
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, SessionState] = {}
        self._order: list[str] = []

    def create(self, user_id: str | None = None) -> SessionState:
        self.purge()
        while len(self._order) >= self.max_sessions:
            oldest = self._order.pop(0)
            self._sessions.pop(oldest, None)
        session_id = uuid.uuid4().hex
        session = SessionState(session_id=session_id, user_id=user_id)
        self._sessions[session_id] = session
        self._order.append(session_id)
        return session

    def get(self, session_id: str) -> SessionState | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if time.monotonic() - session.last_active_at > self.ttl_seconds:
            self._sessions.pop(session_id, None)
            if session_id in self._order:
                self._order.remove(session_id)
            return None
        return session

    def touch(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.last_active_at = time.monotonic()

    def purge(self) -> None:
        """Drop all sessions whose TTL has expired."""
        now = time.monotonic()
        expired = [
            sid
            for sid, s in self._sessions.items()
            if now - s.last_active_at > self.ttl_seconds
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
            if sid in self._order:
                self._order.remove(sid)
