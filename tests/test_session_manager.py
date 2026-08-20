"""Step 2 acceptance: Session Manager lifecycle, collision, concurrent sessions."""

import time
import threading

import pytest

from sentience_governor.session_manager.manager import SessionManager, SessionState


class TestSessionLifecycle:
    def test_session_start_active(self):
        sm = SessionManager()
        sm.session_start(session_id="s1", agent_id="agent-1")
        assert sm.get_state("s1") == SessionState.ACTIVE

    def test_session_end_closed(self):
        sm = SessionManager()
        sm.session_start(session_id="s1", agent_id="agent-1")
        sm.session_end("s1")
        assert sm.get_state("s1") == SessionState.CLOSED

    def test_sequence_numbers_monotonic(self):
        sm = SessionManager()
        sm.session_start(session_id="s1", agent_id="agent-1")
        seqs = []
        for _ in range(5):
            with sm.acquire_sequence("s1") as ctx:
                seqs.append(ctx.next_sequence())
        assert seqs == [1, 2, 3, 4, 5]

    def test_previous_event_id_chain(self):
        sm = SessionManager()
        sm.session_start(session_id="s1", agent_id="agent-1")
        with sm.acquire_sequence("s1") as ctx:
            assert ctx.last_event_id is None
            seq1 = ctx.next_sequence()
            ctx.set_last_event_id("evt-001")
        with sm.acquire_sequence("s1") as ctx:
            assert ctx.last_event_id == "evt-001"


class TestSessionCollision:
    def test_force_close_prior_session(self):
        forced = []

        def on_force(sid, aid):
            forced.append((sid, aid))

        sm = SessionManager(on_session_force_closed=on_force)
        sm.session_start(session_id="s1", agent_id="agent-1")
        assert sm.get_state("s1") == SessionState.ACTIVE

        # Same agent_id, new session_id
        sm.session_start(session_id="s2", agent_id="agent-1")
        assert sm.get_state("s1") == SessionState.CLOSED
        assert sm.get_state("s2") == SessionState.ACTIVE
        assert len(forced) == 1
        assert forced[0] == ("s1", "agent-1")


class TestConcurrentSessions:
    def test_multiple_agents_concurrent(self):
        sm = SessionManager()
        for i in range(5):
            sm.session_start(session_id=f"s{i}", agent_id=f"agent-{i}")
        for i in range(5):
            assert sm.get_state(f"s{i}") == SessionState.ACTIVE

    def test_cross_session_parallel_processing(self):
        sm = SessionManager()
        sm.session_start(session_id="sA", agent_id="agentA")
        sm.session_start(session_id="sB", agent_id="agentB")

        results = {}

        def process(session_id, iterations):
            seqs = []
            for _ in range(iterations):
                with sm.acquire_sequence(session_id) as ctx:
                    seqs.append(ctx.next_sequence())
            results[session_id] = seqs

        t1 = threading.Thread(target=process, args=("sA", 10))
        t2 = threading.Thread(target=process, args=("sB", 10))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["sA"] == list(range(1, 11))
        assert results["sB"] == list(range(1, 11))


class TestConcurrentSessionsPerAgent:
    """v0.3.0.2 — one agent may hold several live sessions when the caller
    opts in. Overlapping LangChain root invocations are governed through one
    handler, so a second root is not a collision."""

    @staticmethod
    def _indexed(sm, agent_id):
        """The agent's live-session set, or None when the agent is not indexed."""
        return sm._agent_sessions.get(agent_id)

    def test_allow_concurrent_keeps_both_sessions_live(self):
        sm = SessionManager()
        sm.session_start(session_id="sA", agent_id="agent-1", allow_concurrent=True)
        sm.session_start(session_id="sB", agent_id="agent-1", allow_concurrent=True)

        assert sm.get_state("sA") == SessionState.ACTIVE
        assert sm.get_state("sB") == SessionState.ACTIVE
        assert self._indexed(sm, "agent-1") == {"sA", "sB"}

    def test_allow_concurrent_fires_no_force_close_callback(self):
        forced = []
        sm = SessionManager(on_session_force_closed=lambda s, a: forced.append(s))
        sm.session_start(session_id="sA", agent_id="agent-1", allow_concurrent=True)
        sm.session_start(session_id="sB", agent_id="agent-1", allow_concurrent=True)
        assert forced == [], "concurrent starts must not force-close anything"

    def test_ending_a_leaves_b_active_and_indexed(self):
        sm = SessionManager()
        sm.session_start(session_id="sA", agent_id="agent-1", allow_concurrent=True)
        sm.session_start(session_id="sB", agent_id="agent-1", allow_concurrent=True)

        sm.session_end("sA")

        assert sm.get_state("sA") == SessionState.CLOSED
        assert sm.get_state("sB") == SessionState.ACTIVE, \
            "ending one session must not close a concurrently live one"
        assert self._indexed(sm, "agent-1") == {"sB"}, \
            "the surviving session must remain indexed for its agent"

    def test_ending_the_last_session_removes_the_agent_from_the_index(self):
        sm = SessionManager()
        sm.session_start(session_id="sA", agent_id="agent-1", allow_concurrent=True)
        sm.session_start(session_id="sB", agent_id="agent-1", allow_concurrent=True)

        sm.session_end("sA")
        sm.session_end("sB")

        assert self._indexed(sm, "agent-1") is None, \
            "an agent with no live sessions must leave nothing in the index"

    def test_default_closes_every_live_session_not_just_one(self):
        """The default path must be deterministic across the whole set: with
        several sessions live, starting a non-concurrent one closes them all."""
        forced = []
        sm = SessionManager(on_session_force_closed=lambda s, a: forced.append(s))
        sm.session_start(session_id="sA", agent_id="agent-1", allow_concurrent=True)
        sm.session_start(session_id="sB", agent_id="agent-1", allow_concurrent=True)
        sm.session_start(session_id="sC", agent_id="agent-1", allow_concurrent=True)

        sm.session_start(session_id="sD", agent_id="agent-1")  # default

        for closed in ("sA", "sB", "sC"):
            assert sm.get_state(closed) == SessionState.CLOSED, \
                f"{closed} must be force-closed, not left behind"
        assert sm.get_state("sD") == SessionState.ACTIVE
        assert self._indexed(sm, "agent-1") == {"sD"}
        assert forced == ["sA", "sB", "sC"], \
            "every prior session is force-closed, in a deterministic order"

    def test_default_still_isolates_different_agents(self):
        sm = SessionManager()
        sm.session_start(session_id="sA", agent_id="agent-1")
        sm.session_start(session_id="sB", agent_id="agent-2")
        assert sm.get_state("sA") == SessionState.ACTIVE
        assert sm.get_state("sB") == SessionState.ACTIVE
