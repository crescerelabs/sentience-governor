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
