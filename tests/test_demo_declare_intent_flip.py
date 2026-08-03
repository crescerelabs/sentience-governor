"""v0.3.0 CP8: the declare_intent BEFORE/AFTER showcase.

Asserts the flip is real (built by the capture-time evaluator, not hand
authored), non-retroactive (pre-declaration POL-001 survives), and measured
by the unchanged analyzer with identical token counts on both sides.
"""

from __future__ import annotations

import argparse

from sentience_governor.cli import ux
from sentience_governor.demos.declare_intent_flip import (
    AFTER_SESSION_ID,
    BEFORE_SESSION_ID,
    OBJECTIVE,
    SCOPE,
    build_after_events,
    build_before_events,
    run_flip_demo,
)


def _scopes(events):
    return [e for e in events if e["event_type"] == "SCOPE_ASSERTED"]


class TestBeforeTrace:
    def test_every_mutating_turn_fires_pol001(self):
        scopes = _scopes(build_before_events())
        assert len(scopes) == 3
        assert all("POL-001" in s["policy_violations"] for s in scopes)


class TestAfterTrace:
    def test_pre_declaration_turn_keeps_pol001_post_do_not(self):
        events = build_after_events()
        scopes = _scopes(events)
        assert len(scopes) == 3
        # Turn 1 (pre-declaration) still fires; turns 2-3 (post) do not.
        assert "POL-001" in scopes[0]["policy_violations"]
        assert "POL-001" not in scopes[1]["policy_violations"]
        assert "POL-001" not in scopes[2]["policy_violations"]

    def test_declaration_event_is_real_and_agent_declared(self):
        events = build_after_events()
        declared = [
            e for e in events
            if e["event_type"] == "INTENT_DECLARED"
            and e["payload"].get("intent_source") != "none"
        ]
        assert len(declared) == 1
        payload = declared[0]["payload"]
        assert payload["stated_objective"] == OBJECTIVE
        assert payload["session_scope_hint"] == SCOPE
        # inferred / inferred_low: agent runtime self-declaration, not explicit.
        assert payload["intent_source"] == "inferred"
        assert payload["intent_confidence"] == "inferred_low"


class TestFlipMeasurement:
    def test_flip_and_identical_token_totals(self):
        r = run_flip_demo()
        assert r.before_scope_pol001 == [True, True, True]
        assert r.after_scope_pol001 == [True, False, False]
        # BEFORE: all compute undeclared; AFTER: only the pre-declaration turn.
        assert r.before_spend["undeclared_percent"] == 100.0
        assert r.after_spend["undeclared_percent"] == 37.5
        # Identical token totals -> the delta is the POL-001 flip, not numbers.
        assert r.before_spend["total_tokens"] == r.after_spend["total_tokens"]
        assert r.after_spend["undeclared_turn_count"] == 1

    def test_pre_declaration_event_unchanged_vs_baseline(self):
        # The AFTER pre-declaration SCOPE equals a BEFORE SCOPE in flags: the
        # declaration is never applied backwards.
        before0 = _scopes(build_before_events())[0]
        after0 = _scopes(build_after_events())[0]
        assert before0["policy_violations"] == after0["policy_violations"]
        assert before0["advisory_flags"] == after0["advisory_flags"]

    def test_same_analyzer_on_both_sides(self):
        # Both spends carry the analyzer's normal keys (status ok), i.e. the
        # unchanged compute_undeclared_intent_spend ran on each trace.
        r = run_flip_demo()
        assert r.before_spend["status"] == "ok"
        assert r.after_spend["status"] == "ok"


class TestDemoCommand:
    def test_declare_intent_demo_prints_before_after(self, capsys):
        rc = ux.run_demo(argparse.Namespace(demo_name="declare-intent"))
        assert rc == 0
        out = capsys.readouterr().out
        assert "BEFORE (no declaration):" in out
        assert "AFTER (declared after turn 1):" in out
        assert "100.0%" in out and "37.5%" in out
        assert "non-retroactive" in out
        assert "No analyzer change" in out
        assert "—" not in out  # em-dash-free copy convention

    def test_session_ids_are_stable(self):
        assert BEFORE_SESSION_ID == "demo-declare-intent-before"
        assert AFTER_SESSION_ID == "demo-declare-intent-after"
