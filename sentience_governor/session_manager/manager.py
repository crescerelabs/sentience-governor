"""Session Manager — IDLE → ACTIVE → CLOSING → CLOSED state machine.

Lock ownership
--------------
SessionManager owns:
  - per-session sequencing lock  (_session_locks[session_id])
  - session registry write lock  (_registry_lock)

It does NOT own the cache-level lock (owned by InProcessCache).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)

INACTIVITY_TIMEOUT_SECONDS = 600  # 10 minutes rolling


class SessionState(str, Enum):
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class _SessionEntry:
    """Internal record held per session in the registry."""

    __slots__ = (
        "session_id",
        "agent_id",
        "state",
        "sequence_number",
        "last_event_id",
        "last_activity",
        "lock",  # per-session sequencing lock (threading.Lock for sync; asyncio.Lock for async)
        "profile",  # GovernanceProfile or None — v0.2.5; immutable per session
    )

    def __init__(
        self,
        session_id: str,
        agent_id: str,
        profile: Optional[object] = None,
    ) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.state: SessionState = SessionState.IDLE
        self.sequence_number: int = 0
        self.last_event_id: Optional[str] = None
        self.last_activity: float = time.monotonic()
        self.lock = threading.Lock()  # per-session sequencing lock
        # v0.2.5: governance profile for this session. Stored by
        # reference; never mutated after session_start. Typed as
        # `object` to avoid an import cycle (the profile module
        # imports schema constants, not the session manager).
        self.profile = profile


class SessionManager:
    """Manages session lifecycles for all agents.

    Thread-safety
    -------------
    * Session registry (_sessions) is protected by _registry_lock (RLock).
    * Per-session event sequencing is protected by each session's own lock.
    * Multiple sessions can be processed concurrently.
    * A single session is processed sequentially under its lock.
    """

    def __init__(
        self,
        inactivity_timeout: int = INACTIVITY_TIMEOUT_SECONDS,
        on_session_force_closed: Optional[object] = None,  # callback(session_id, agent_id)
    ) -> None:
        self._sessions: Dict[str, _SessionEntry] = {}
        self._agent_to_session: Dict[str, str] = {}  # agent_id → active session_id
        self._registry_lock = threading.RLock()
        self._inactivity_timeout = inactivity_timeout
        self._on_session_force_closed = on_session_force_closed

        # Background inactivity reaper
        self._reaper_thread = threading.Thread(
            target=self._reaper_loop, daemon=True, name="sentience-session-reaper"
        )
        self._reaper_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def session_start(
        self,
        session_id: str,
        agent_id: str,
        initial_sequence: int = 0,
        initial_last_event_id: Optional[str] = None,
        profile: Optional[object] = None,
    ) -> None:
        """Transition session to ACTIVE.  Handles collision (force-close prior).

        Parameters
        ----------
        initial_sequence, initial_last_event_id
            Pre-seed chain state when resuming a session whose prior events
            already exist on disk (see
            ``sentience_governor.session_manager.resumption``). Defaults of
            ``0`` / ``None`` preserve the original fresh-session behaviour
            for every existing caller (MCP wrapper, LangChain handler,
            tests). Only the Claude Code hook uses the non-default values
            today; the MCP wrapper will follow when the Parking Lot item
            for process-restart continuity is pulled.
        profile
            Optional ``GovernanceProfile`` for this session (v0.2.5). When
            ``None`` (default), the session runs without profile
            governance — backward-compatible with v0.2.4 callers. When
            non-None, the profile is stored by reference on the session
            entry and consulted by ``EventBuilder`` for profile-aware
            policy evaluation. The profile is immutable for the session's
            lifetime; mid-session changes to ``~/.sentience/profile.yaml``
            do not affect any already-active session.
        """
        with self._registry_lock:
            # Collision: same agent_id already has an active session
            prior_session_id = self._agent_to_session.get(agent_id)
            if prior_session_id and prior_session_id != session_id:
                prior = self._sessions.get(prior_session_id)
                if prior and prior.state == SessionState.ACTIVE:
                    logger.warning(
                        "SESSION_FORCE_CLOSED: agent %s had active session %s; "
                        "force-closing before starting %s",
                        agent_id,
                        prior_session_id,
                        session_id,
                    )
                    self._force_close(prior)
                    if self._on_session_force_closed:
                        try:
                            self._on_session_force_closed(prior_session_id, agent_id)
                        except Exception:
                            pass

            entry = _SessionEntry(
                session_id=session_id, agent_id=agent_id, profile=profile
            )
            entry.state = SessionState.ACTIVE
            entry.sequence_number = initial_sequence
            entry.last_event_id = initial_last_event_id
            self._sessions[session_id] = entry
            self._agent_to_session[agent_id] = session_id

    def session_end(self, session_id: str) -> None:
        """Transition session ACTIVE → CLOSING → CLOSED."""
        with self._registry_lock:
            entry = self._sessions.get(session_id)
            if entry is None or entry.state == SessionState.CLOSED:
                return
            entry.state = SessionState.CLOSING
            self._close_entry(entry)

    def get_state(self, session_id: str) -> Optional[SessionState]:
        with self._registry_lock:
            entry = self._sessions.get(session_id)
            return entry.state if entry else None

    def get_profile(self, session_id: str) -> Optional[object]:
        """Return the ``GovernanceProfile`` attached at session_start, or None.

        Returned by reference (not a copy) — the profile object itself is
        immutable from the loader's side (frozen ``_data`` dict). Callers
        MUST NOT mutate the returned profile. Returns None for sessions
        not under this manager and for sessions started without a
        profile.
        """
        with self._registry_lock:
            entry = self._sessions.get(session_id)
            return entry.profile if entry else None

    def acquire_sequence(self, session_id: str) -> "_SequenceContext":
        """Return a context manager that holds the per-session sequencing lock."""
        with self._registry_lock:
            entry = self._sessions.get(session_id)
        if entry is None:
            raise KeyError(f"No active session: {session_id}")
        return _SequenceContext(entry)

    def touch(self, session_id: str) -> None:
        """Reset the inactivity timer for a session."""
        with self._registry_lock:
            entry = self._sessions.get(session_id)
        if entry:
            entry.last_activity = time.monotonic()

    @asynccontextmanager
    async def async_session(
        self,
        session_id: str,
        agent_id: str,
        stated_objective: Optional[str] = None,
    ) -> AsyncIterator["_AsyncSessionHandle"]:
        """Async context manager wrapping session_start / session_end."""
        self.session_start(session_id=session_id, agent_id=agent_id)
        handle = _AsyncSessionHandle(
            session_id=session_id,
            agent_id=agent_id,
            stated_objective=stated_objective,
            manager=self,
        )
        try:
            yield handle
        finally:
            self.session_end(session_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _force_close(self, entry: _SessionEntry) -> None:
        self._close_entry(entry)

    def _close_entry(self, entry: _SessionEntry) -> None:
        entry.state = SessionState.CLOSED
        self._agent_to_session.pop(entry.agent_id, None)

    def _reaper_loop(self) -> None:
        """Periodically close sessions that have exceeded inactivity timeout."""
        while True:
            time.sleep(30)  # check every 30 s
            now = time.monotonic()
            with self._registry_lock:
                to_close = [
                    e
                    for e in self._sessions.values()
                    if e.state == SessionState.ACTIVE
                    and (now - e.last_activity) >= self._inactivity_timeout
                ]
            for entry in to_close:
                logger.info(
                    "Inactivity timeout: closing session %s (agent %s)",
                    entry.session_id,
                    entry.agent_id,
                )
                with self._registry_lock:
                    self._close_entry(entry)


class _SequenceContext:
    """Context manager holding the per-session lock and exposing next_seq()."""

    def __init__(self, entry: _SessionEntry) -> None:
        self._entry = entry

    def __enter__(self) -> "_SequenceContext":
        self._entry.lock.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self._entry.lock.release()

    def next_sequence(self) -> int:
        self._entry.sequence_number += 1
        return self._entry.sequence_number

    @property
    def last_event_id(self) -> Optional[str]:
        return self._entry.last_event_id

    def set_last_event_id(self, event_id: str) -> None:
        self._entry.last_event_id = event_id


class _AsyncSessionHandle:
    """Thin handle returned by async_session() context manager."""

    def __init__(
        self,
        session_id: str,
        agent_id: str,
        stated_objective: Optional[str],
        manager: SessionManager,
    ) -> None:
        self.session_id = session_id
        self.agent_id = agent_id
        self.stated_objective = stated_objective
        self._manager = manager
