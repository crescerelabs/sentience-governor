"""CP2 — obligations 1-11 from design 0001.

Defect-driving tests assert the CONTRACT in the spec and are expected to FAIL
against v0.3.0.1. Regression tests may already pass; that is intended.
"""
import asyncio
import threading

from conftest import (as_dict, make_handler, pol001, sessions, events_for, rid)


# ---------------------------------------------------------------- 1
def test_01_nested_start_creates_no_second_session_and_no_false_pol001():
    """DEFECT: nested on_chain_start resets the session, and the new session
    never receives an intent baseline, so an in-scope tool fires POL-001."""
    sink, h = make_handler(declared=["read_thing"])
    root, node = rid(), rid()
    h.on_chain_start({"name": "root"}, {"input": "x"}, run_id=root, parent_run_id=None)
    h.on_chain_start({"name": "node"}, {"input": "y"}, run_id=node, parent_run_id=root)
    h.on_tool_start({"name": "read_thing"}, "alpha", run_id=rid(), parent_run_id=node)
    h.on_tool_end("done", run_id=rid(), parent_run_id=node)

    assert len(set(sessions(sink.events))) == 1, "nested start must not create a second session"
    assert pol001(sink.events) == [], "in-scope tool under a nested start must not fire POL-001"


# ---------------------------------------------------------------- 2
def test_02_nested_end_does_not_tear_down_governance():
    """DEFECT: the first nested on_chain_end clears the session while the outer
    graph is still running, so later tool calls are ungoverned."""
    sink, h = make_handler(declared=["read_thing"])
    root, node = rid(), rid()
    h.on_chain_start({"name": "root"}, {"input": "x"}, run_id=root, parent_run_id=None)
    h.on_chain_start({"name": "node"}, {"input": "y"}, run_id=node, parent_run_id=root)
    h.on_chain_end({"out": 1}, run_id=node, parent_run_id=root)          # nested end
    before = len(sink.events)
    h.on_tool_start({"name": "read_thing"}, "after-nested-end",
                    run_id=rid(), parent_run_id=root)

    assert len(sink.events) > before, "tool after a nested end must still be governed"
    assert pol001(sink.events) == [], "governance must survive a nested end"


# ---------------------------------------------------------------- 3
def test_03_two_overlapping_roots_are_both_governed():
    """DEFECT: one handler, two legitimate roots open at once. Neither may be
    swallowed and their events must not share a session."""
    sink, h = make_handler(declared=["read_thing"])
    a, b = rid(), rid()
    h.on_chain_start({"name": "A"}, {}, run_id=a, parent_run_id=None)
    h.on_chain_start({"name": "B"}, {}, run_id=b, parent_run_id=None)     # second ROOT
    h.on_tool_start({"name": "read_thing"}, "a", run_id=rid(), parent_run_id=a)
    h.on_tool_start({"name": "read_thing"}, "b", run_id=rid(), parent_run_id=b)

    sids = [s for s in sessions(sink.events) if s]
    assert len(set(sids)) == 2, "two overlapping roots must yield two sessions"
    assert pol001(sink.events) == [], "neither root may lose its baseline"


# ---------------------------------------------------------------- 4
def test_04_two_overlapping_roots_async():
    """Same as 03 under asyncio interleaving."""
    sink, h = make_handler(declared=["read_thing"])
    a, b = rid(), rid()

    async def run_root(root_id, tag):
        h.on_chain_start({"name": tag}, {}, run_id=root_id, parent_run_id=None)
        await asyncio.sleep(0.02)
        h.on_tool_start({"name": "read_thing"}, tag, run_id=rid(), parent_run_id=root_id)
        h.on_chain_end({}, run_id=root_id, parent_run_id=None)

    asyncio.run(asyncio.gather(run_root(a, "A"), run_root(b, "B")) if False
                else _gather(run_root, a, b))
    sids = [s for s in sessions(sink.events) if s]
    assert len(set(sids)) == 2, "overlapping async roots must yield two sessions"
    assert pol001(sink.events) == []


async def _gather(fn, a, b):
    await asyncio.gather(fn(a, "A"), fn(b, "B"))


# ---------------------------------------------------------------- 5
def test_05_ending_root_a_leaves_root_b_live():
    """DEFECT: on_chain_end tears down unconditionally, so ending A kills B."""
    sink, h = make_handler(declared=["read_thing"])
    a, b = rid(), rid()
    h.on_chain_start({"name": "A"}, {}, run_id=a, parent_run_id=None)
    h.on_chain_start({"name": "B"}, {}, run_id=b, parent_run_id=None)
    h.on_chain_end({}, run_id=a, parent_run_id=None)                      # A ends
    before = len(sink.events)
    h.on_tool_start({"name": "read_thing"}, "b-after-a-end", run_id=rid(), parent_run_id=b)

    assert len(sink.events) > before, "root B must still emit after A ends"
    assert pol001(sink.events) == [], "B must retain its baseline after A ends"


# ---------------------------------------------------------------- 6
def test_06_sequential_reuse_is_correct():
    """REGRESSION: A completes, then B roots cleanly with its own session."""
    sink, h = make_handler(declared=["read_thing"])
    a, b = rid(), rid()
    h.on_chain_start({"name": "A"}, {}, run_id=a, parent_run_id=None)
    h.on_tool_start({"name": "read_thing"}, "a", run_id=rid(), parent_run_id=a)
    h.on_chain_end({}, run_id=a, parent_run_id=None)
    h.on_chain_start({"name": "B"}, {}, run_id=b, parent_run_id=None)
    h.on_tool_start({"name": "read_thing"}, "b", run_id=rid(), parent_run_id=b)

    sids = [s for s in sessions(sink.events) if s]
    assert len(set(sids)) == 2, "sequential roots get distinct sessions"
    assert pol001(sink.events) == []


# ---------------------------------------------------------------- 7
def test_07_deep_ancestry_resolves_to_true_root():
    """DEFECT: a tool three levels below the root must attach to the root."""
    sink, h = make_handler(declared=["read_thing"])
    root, sub, node = rid(), rid(), rid()
    h.on_chain_start({"name": "root"}, {}, run_id=root, parent_run_id=None)
    h.on_chain_start({"name": "sub"}, {}, run_id=sub, parent_run_id=root)
    h.on_chain_start({"name": "node"}, {}, run_id=node, parent_run_id=sub)
    h.on_tool_start({"name": "read_thing"}, "deep", run_id=rid(), parent_run_id=node)

    assert len(set(sessions(sink.events))) == 1, "deep nesting must not fragment the session"
    assert pol001(sink.events) == []


# ---------------------------------------------------------------- 8
def test_08_unresolvable_tool_event_does_not_invent_a_session():
    """CONTRACT: a tool event with no resolvable root must no-op, not attach."""
    sink, h = make_handler(declared=["read_thing"])
    h.on_tool_start({"name": "read_thing"}, "orphan",
                    run_id=rid(), parent_run_id=rid())   # parent never seen
    assert sink.events == [], "an unresolvable tool event must emit nothing"


# ---------------------------------------------------------------- 9
def test_09_flat_undeclared_tool_still_fires_pol001():
    """REGRESSION: the true positive must survive the fix."""
    sink, h = make_handler(declared=["other.thing"])
    root = rid()
    h.on_chain_start({"name": "root"}, {}, run_id=root, parent_run_id=None)
    h.on_tool_start({"name": "read_thing"}, "x", run_id=rid(), parent_run_id=root)
    assert pol001(sink.events), "undeclared tool must still fire POL-001"


# ---------------------------------------------------------------- 10
def test_10_no_callback_ids_behaves_as_today():
    """REGRESSION (legacy path): no run ids supplied -> current behaviour."""
    sink, h = make_handler(declared=["read_thing"])
    h.on_chain_start({"name": "root"}, {"input": "x"})
    h.on_tool_start({"name": "read_thing"}, "x")
    h.on_tool_end("done")
    h.on_chain_end({"out": 1})
    assert sink.events, "legacy path must still emit"
    assert pol001(sink.events) == [], "legacy flat chain: in-scope tool, no POL-001"


# ---------------------------------------------------------------- 24
def test_24_overlapping_roots_stay_independently_active_in_the_manager(caplog):
    """CP3 extension: root isolation must hold in the session registry too,
    not only in the emitted trace.

    Before v0.3.0.2 the SessionManager allowed one active session per
    agent_id, so opening root B force-closed root A while A was still
    running and still emitting.
    """
    import logging
    from sentience_governor.session_manager import SessionState

    sink, h = make_handler(declared=["read_thing"])
    a, b = rid(), rid()
    with caplog.at_level(logging.WARNING):
        h.on_chain_start({"name": "A"}, {}, run_id=a, parent_run_id=None)
        h.on_chain_start({"name": "B"}, {}, run_id=b, parent_run_id=None)

        sid_a = h._roots[a].session_id
        sid_b = h._roots[b].session_id
        assert h._sm.get_state(sid_a) == SessionState.ACTIVE, \
            "root A must stay ACTIVE after root B opens"
        assert h._sm.get_state(sid_b) == SessionState.ACTIVE

        h.on_chain_end({}, run_id=a, parent_run_id=None)
        assert h._sm.get_state(sid_a) == SessionState.CLOSED
        assert h._sm.get_state(sid_b) == SessionState.ACTIVE, \
            "ending root A must not close root B"

        h.on_chain_end({}, run_id=b, parent_run_id=None)
        assert h._sm.get_state(sid_b) == SessionState.CLOSED

    assert "SESSION_FORCE_CLOSED" not in caplog.text, \
        "overlapping roots are legitimate, not a session collision"
