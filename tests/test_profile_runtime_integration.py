"""Tests for v0.2.5 CP2 — profile-driven runtime integration.

Covers the integration of governance profiles into the policy
evaluator (EventBuilder._apply_profile_to_scope) and the session
manager wiring (SessionManager.session_start profile param,
SessionManager.get_profile accessor).

Critical guarantees verified:

* Sessions started WITHOUT a profile produce v0.2.4-identical
  trace output (backward-compat regression guard).
* ``demand_at: session_start`` (v0.2.4 default in defaults profile)
  fires POL-001 on every mutating event without intent.
* ``demand_at: first_write`` fires POL-001 exactly once per session.
* ``demand_at: never`` suppresses POL-001 unconditionally.
* TASK_BOUNDARY_CROSSED fires for each of the four signals
  (dir_change, file_type_shift, time_gap, read_to_write_transition)
  when the signal is configured.
* HIGH_CONSEQUENCE_DETECTED fires when tool:target composite
  matches an operator-authored regex pattern, and does NOT fire
  when no pattern matches.
* Malformed high-consequence patterns are skipped silently at
  runtime (operator already warned at load time).

All tests run hermetically using in-memory builders + caches; no
filesystem profiles are loaded.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import (
    EventBuilder,
    _detect_task_boundary,
    _extract_dir,
    _extract_file_ext,
)
from sentience_governor.profile import (
    DEMAND_AT_FIRST_WRITE,
    DEMAND_AT_NEVER,
    DEMAND_AT_SESSION_START,
    GovernanceProfile,
    SIGNAL_DIR_CHANGE,
    SIGNAL_FILE_TYPE_SHIFT,
    SIGNAL_READ_TO_WRITE_TRANSITION,
    SIGNAL_TIME_GAP,
)
from sentience_governor.profile.schema import default_profile_data
from sentience_governor.schema.events import (
    AdvisoryFlag,
    DeploymentMode,
    IntentConfidence,
    IntentSource,
    OperationType,
    PolicyViolation,
)
from sentience_governor.session_manager.manager import SessionManager


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


def _make_builder(
    agent_id: str,
    session_id: str,
    profile: Optional[GovernanceProfile] = None,
) -> tuple:
    """Construct a SessionManager + cache + EventBuilder triple.

    Mirrors tests/test_event_builder.py's helper but accepts an
    optional profile that gets attached at session_start.
    """
    sm = SessionManager()
    cache = InProcessCache()
    sm.session_start(session_id=session_id, agent_id=agent_id, profile=profile)
    cache.init_session(session_id)
    builder = EventBuilder(
        session_manager=sm,
        cache=cache,
        agent_id=agent_id,
        session_id=session_id,
        deployment_mode=DeploymentMode.vendor_managed,
    )
    return sm, cache, builder


def _profile_with(
    *,
    demand_at: str = DEMAND_AT_SESSION_START,
    signals: Optional[List[str]] = None,
    time_gap_seconds: int = 300,
    dir_change_depth: int = 2,
    hc_tools: Optional[List[str]] = None,
) -> GovernanceProfile:
    """Build a GovernanceProfile with the given overrides on top of defaults."""
    data = default_profile_data()
    data["session_intent"]["demand_at"] = demand_at
    data["task_boundary"]["signals"] = list(signals or [])
    data["task_boundary"]["time_gap_seconds"] = time_gap_seconds
    data["task_boundary"]["dir_change_depth"] = dir_change_depth
    data["high_consequence"]["tools"] = list(hc_tools or [])
    return GovernanceProfile(data)


# ---------------------------------------------------------------------------
# Backward-compat regression guard
# ---------------------------------------------------------------------------


def test_session_without_profile_matches_v0_2_4_behavior():
    """No profile attached → identical advisory_flags + policy_violations
    to v0.2.4: POL-001 fires on every mutating op without intent.

    This is the critical regression guard from the plan: v0.2.5
    must be byte-stable for v0.2.4-shaped sessions.
    """
    sm, cache, builder = _make_builder("agent-1", "sess-1", profile=None)
    # No INTENT_DECLARED → first mutating op fires POL-001.
    e1 = builder.build_agent_registered(
        agent_version="1.0",
        vendor_id="v1",
        declared_capabilities=["fs.write"],
        owner_claim="u1",
    )
    assert PolicyViolation.POL_001 not in (e1.policy_violations or [])

    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="src/foo.py",
        operation_type=OperationType.WRITE,
    )
    assert PolicyViolation.POL_001 in e2.policy_violations

    # Second mutating op still fires (v0.2.4 behavior is per-event).
    e3 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="src/bar.py",
        operation_type=OperationType.WRITE,
    )
    assert PolicyViolation.POL_001 in e3.policy_violations

    # And no new v0.2.5 advisory flags should appear without a profile.
    for ev in (e2, e3):
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in ev.advisory_flags
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED not in ev.advisory_flags


# ---------------------------------------------------------------------------
# demand_at gating
# ---------------------------------------------------------------------------


def test_demand_at_session_start_fires_pol_001_per_event():
    """v0.2.4 default behavior — POL-001 fires every event without intent."""
    profile = _profile_with(demand_at=DEMAND_AT_SESSION_START)
    sm, cache, builder = _make_builder("agent-2", "sess-2", profile=profile)
    e1 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="a.py",
        operation_type=OperationType.WRITE,
    )
    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="b.py",
        operation_type=OperationType.WRITE,
    )
    assert PolicyViolation.POL_001 in e1.policy_violations
    assert PolicyViolation.POL_001 in e2.policy_violations


def test_demand_at_first_write_fires_pol_001_once_per_session():
    """first_write mode: POL-001 fires on first mutating event only."""
    profile = _profile_with(demand_at=DEMAND_AT_FIRST_WRITE)
    sm, cache, builder = _make_builder("agent-3", "sess-3", profile=profile)
    # First mutating event — POL-001 fires
    e1 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="a.py",
        operation_type=OperationType.WRITE,
    )
    # Second mutating event — POL-001 suppressed
    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="b.py",
        operation_type=OperationType.WRITE,
    )
    # Third mutating event — still suppressed
    e3 = builder.build_scope_asserted(
        tool_id="db.delete",
        asserted_permissions=["delete"],
        target_system="users",
        operation_type=OperationType.DELETE,
    )
    assert PolicyViolation.POL_001 in e1.policy_violations
    assert PolicyViolation.POL_001 not in e2.policy_violations
    assert PolicyViolation.POL_001 not in e3.policy_violations
    # The SCOPE_OPERATION_UNEXPECTED advisory flag remains on the
    # suppressed events — only the policy violation is gated.
    assert AdvisoryFlag.SCOPE_OPERATION_UNEXPECTED in e2.advisory_flags
    assert AdvisoryFlag.SCOPE_OPERATION_UNEXPECTED in e3.advisory_flags


def test_demand_at_never_suppresses_pol_001():
    """never mode: POL-001 never appears in policy_violations."""
    profile = _profile_with(demand_at=DEMAND_AT_NEVER)
    sm, cache, builder = _make_builder("agent-4", "sess-4", profile=profile)
    e1 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="a.py",
        operation_type=OperationType.WRITE,
    )
    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="b.py",
        operation_type=OperationType.WRITE,
    )
    assert PolicyViolation.POL_001 not in e1.policy_violations
    assert PolicyViolation.POL_001 not in e2.policy_violations
    # The advisory flag stays — operator only opted out of the
    # policy_violation, not the observability signal.
    assert AdvisoryFlag.SCOPE_OPERATION_UNEXPECTED in e1.advisory_flags


# ---------------------------------------------------------------------------
# Task-boundary signal detection
# ---------------------------------------------------------------------------


def test_task_boundary_dir_change_fires_on_directory_shift():
    profile = _profile_with(
        demand_at=DEMAND_AT_NEVER,  # suppress POL-001 to isolate the boundary flag
        signals=[SIGNAL_DIR_CHANGE],
        dir_change_depth=2,
    )
    sm, cache, builder = _make_builder("agent-5", "sess-5", profile=profile)
    # First event — no boundary (no prior baseline).
    e1 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="src/foo/bar.py",
        operation_type=OperationType.WRITE,
    )
    # Second event — same dir, no boundary.
    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="src/foo/baz.py",
        operation_type=OperationType.WRITE,
    )
    # Third event — different top-level dir, boundary fires.
    e3 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="tests/foo/quux.py",
        operation_type=OperationType.WRITE,
    )
    assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in e1.advisory_flags
    assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in e2.advisory_flags
    assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in e3.advisory_flags


def test_task_boundary_file_type_shift():
    profile = _profile_with(
        demand_at=DEMAND_AT_NEVER,
        signals=[SIGNAL_FILE_TYPE_SHIFT],
    )
    sm, cache, builder = _make_builder("agent-6", "sess-6", profile=profile)
    e1 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="src/a.py",
        operation_type=OperationType.WRITE,
    )
    # Same extension — no shift.
    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="src/b.py",
        operation_type=OperationType.WRITE,
    )
    # Different extension — shift fires.
    e3 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="docs/readme.md",
        operation_type=OperationType.WRITE,
    )
    assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in e2.advisory_flags
    assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in e3.advisory_flags


def test_task_boundary_time_gap():
    """time_gap signal uses monotonic clock; we exercise the pure helper directly
    to avoid sleeping for 5 minutes in tests."""

    # Pure-function exercise: prior state with old monotonic, current with new.
    class _FakeState:
        last_target_system = "a"
        last_target_dir = "a"
        last_file_ext = "py"
        last_operation_type = "WRITE"
        last_scope_activity_monotonic = 1000.0

    # 600s gap, threshold 300 — should fire.
    assert _detect_task_boundary(
        signals=[SIGNAL_TIME_GAP],
        prior_state=_FakeState(),
        current_target_system="b.py",
        current_operation_type=OperationType.WRITE,
        current_monotonic=1600.0,
        time_gap_seconds=300,
        dir_change_depth=2,
    ) is True

    # 100s gap, threshold 300 — should NOT fire.
    assert _detect_task_boundary(
        signals=[SIGNAL_TIME_GAP],
        prior_state=_FakeState(),
        current_target_system="b.py",
        current_operation_type=OperationType.WRITE,
        current_monotonic=1100.0,
        time_gap_seconds=300,
        dir_change_depth=2,
    ) is False


def test_task_boundary_read_to_write_transition():
    profile = _profile_with(
        demand_at=DEMAND_AT_NEVER,
        signals=[SIGNAL_READ_TO_WRITE_TRANSITION],
    )
    sm, cache, builder = _make_builder("agent-7", "sess-7", profile=profile)
    # First a READ, then a WRITE — transition fires on the WRITE.
    e1 = builder.build_scope_asserted(
        tool_id="fs.read",
        asserted_permissions=["read"],
        target_system="src/a.py",
        operation_type=OperationType.READ,
    )
    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="src/a.py",
        operation_type=OperationType.WRITE,
    )
    assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in e1.advisory_flags
    assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in e2.advisory_flags


# ---------------------------------------------------------------------------
# High-consequence pattern detection
# ---------------------------------------------------------------------------


def test_high_consequence_pattern_match():
    """Tool:target composite matches an operator-authored regex."""
    profile = _profile_with(
        demand_at=DEMAND_AT_NEVER,
        hc_tools=["Bash:.*rm.*-rf.*", "fs.write:.*outside_project.*"],
    )
    sm, cache, builder = _make_builder("agent-8", "sess-8", profile=profile)
    # Match: Bash + dangerous rm
    e1 = builder.build_scope_asserted(
        tool_id="Bash",
        asserted_permissions=["execute"],
        target_system="rm -rf /tmp/scratch",
        operation_type=OperationType.EXECUTE,
    )
    # No match: ordinary fs.write inside project
    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="src/a.py",
        operation_type=OperationType.WRITE,
    )
    assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED in e1.advisory_flags
    assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED not in e2.advisory_flags


def test_high_consequence_malformed_pattern_skipped_silently():
    """Malformed regex in profile is skipped — runtime must not crash."""
    profile = _profile_with(
        demand_at=DEMAND_AT_NEVER,
        hc_tools=["[invalid(", "Bash:dangerous"],  # first bad, second good
    )
    sm, cache, builder = _make_builder("agent-9", "sess-9", profile=profile)
    e1 = builder.build_scope_asserted(
        tool_id="Bash",
        asserted_permissions=["execute"],
        target_system="dangerous-command",
        operation_type=OperationType.EXECUTE,
    )
    # Bad pattern skipped; good pattern still matches.
    assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED in e1.advisory_flags


# ---------------------------------------------------------------------------
# SessionManager profile wiring
# ---------------------------------------------------------------------------


def test_session_manager_get_profile_returns_attached_profile():
    profile = _profile_with(demand_at=DEMAND_AT_FIRST_WRITE)
    sm = SessionManager()
    sm.session_start(session_id="s1", agent_id="a1", profile=profile)
    assert sm.get_profile("s1") is profile
    # Unknown session → None.
    assert sm.get_profile("nope") is None


def test_session_manager_default_no_profile_is_none():
    sm = SessionManager()
    sm.session_start(session_id="s2", agent_id="a2")
    assert sm.get_profile("s2") is None


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


def test_extract_dir_and_file_ext_helpers():
    assert _extract_dir("src/foo/bar.py", 2) == "src/foo"
    assert _extract_dir("src/foo/bar.py", 1) == "src"
    assert _extract_dir("sentience_governor.profile.loader", 2) == "sentience_governor/profile"
    assert _extract_dir("", 2) is None
    assert _extract_dir("a", 2) == "a"  # depth exceeds components

    assert _extract_file_ext("src/foo/bar.py") == "py"
    assert _extract_file_ext("README") is None
    assert _extract_file_ext("foo.tar.gz") == "gz"
    assert _extract_file_ext("") is None
