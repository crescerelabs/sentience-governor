"""EventBuilder runtime tolerance (core 0.3.2.1, issue #19).

The binding path refuses an invalid profile before it reaches the runtime.
Behind that, the builder reads the profile once per session through a
checked view of the raw data and, for any field that is still malformed,
substitutes the default the validator would have insisted on, warns once
per session per field, and never derives policy from the malformed value.
The malformed-profile corpus pins the outcome per fixture; these tests pin
the mechanism and the distinction the operator asked for: an invalid
parameter must not manufacture a boundary, while a legitimate signal in the
same profile still behaves as it always has.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder import builder as builder_module
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile import GovernanceProfile
from sentience_governor.schema.events import AdvisoryFlag, OperationType, PolicyViolation
from sentience_governor.session_manager.manager import SessionManager

SID = "s-tolerance"
TAG = "PROFILE_FIELD_IGNORED"


def _governed(profile_data: Dict[str, Any], *, source: bool = True) -> Tuple[EventBuilder, Any, SessionManager]:
    """A session with ``profile_data`` attached through the test seam (the
    runtime refuses an invalid profile at session_start; this is the layer
    behind that refusal)."""
    from pathlib import Path

    profile = GovernanceProfile(profile_data, source_path=Path("/tmp/tolerance.yaml") if source else None)
    sm, cache = SessionManager(), InProcessCache()
    sm.session_start(SID, "agent")
    cache.init_session(SID)
    sm._sessions[SID].profile = profile  # noqa: SLF001 - deliberate test seam
    b = EventBuilder(sm, cache, "agent", SID)
    reg = b.build_agent_registered(agent_version=None, vendor_id="t", declared_capabilities=[], owner_claim=None)
    return b, reg, sm


def _scope(b: EventBuilder, tool: str, target: str, op: OperationType, use_id: str):
    return b.build_scope_asserted(tool_id=tool, asserted_permissions=[], target_system=target, operation_type=op, tool_use_id=use_id)


def _two_dirs(b: EventBuilder) -> List[Any]:
    return [
        _scope(b, "fs_read", "filesystem/a/b.txt", OperationType.READ, "t1"),
        _scope(b, "fs_write", "filesystem/x/y.py", OperationType.WRITE, "t2"),
        _scope(b, "Bash", "shell", OperationType.EXECUTE, "t3"),
    ]


def _flags(events: List[Any]) -> set:
    return {f for e in events for f in e.advisory_flags}


def _tolerance_warnings(caplog, field: str | None = None) -> List[str]:
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and TAG in r.getMessage()]
    return [m for m in msgs if field is None or f" {field} " in m]


VALID = {
    "schema_version": 1,
    "session_intent": {"demand_at": "first_write"},
    "task_boundary": {"signals": ["dir_change"], "dir_change_depth": 2, "time_gap_seconds": 120},
    "high_consequence": {"tools": ["Bash:shell"]},
}


class TestValidProfileIsUntouched:
    def test_no_tolerance_warnings_and_same_policy_as_before(self, caplog):
        with caplog.at_level(logging.WARNING):
            b, reg, _ = _governed(VALID)
            events = _two_dirs(b)
        assert _tolerance_warnings(caplog) == []
        assert reg.payload.profile_schema_version == 1
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in events[1].advisory_flags  # a/ -> x/ under depth 2
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED in events[2].advisory_flags  # Bash:shell
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED not in events[1].advisory_flags

    def test_view_is_built_once_per_session_and_rebuilt_if_the_profile_changes(self):
        b, _, sm = _governed(VALID)
        profile = sm.get_profile(SID)
        first = b._checked_profile(profile)
        assert b._checked_profile(profile) is first
        other = GovernanceProfile(dict(VALID))
        sm._sessions[SID].profile = other  # noqa: SLF001
        assert b._checked_profile(other) is not first

    def test_missing_schema_version_is_recorded_as_the_default_as_before(self):
        data = {k: v for k, v in VALID.items() if k != "schema_version"}
        _, reg, _ = _governed(data)
        assert reg.payload.profile_schema_version == 1


class TestInvalidParameterDoesNotManufacturePolicy:
    def test_invalid_time_gap_does_not_cross_a_boundary(self, caplog):
        data = {"schema_version": 1, "task_boundary": {"signals": ["time_gap"], "time_gap_seconds": "soon"}}
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed(data)
            events = _two_dirs(b)
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in _flags(events)
        assert len(_tolerance_warnings(caplog, "task_boundary.time_gap_seconds")) == 1

    def test_negative_time_gap_takes_the_default_not_zero(self, caplog, monkeypatch):
        # With the malformed value taken literally (-1) every event would be a
        # boundary. The substituted 300 s must be what decides.
        data = {"schema_version": 1, "task_boundary": {"signals": ["time_gap"], "time_gap_seconds": -1}}
        clock = [1000.0]
        monkeypatch.setattr(builder_module.time, "monotonic", lambda: clock[0])
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed(data)
            events = []
            for i, target in enumerate(["filesystem/a/b.txt", "filesystem/a/c.txt", "filesystem/a/d.txt"]):
                clock[0] += 1.0  # one second apart: far under the substituted 300 s
                events.append(_scope(b, "fs_write", target, OperationType.WRITE, f"t{i}"))
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in _flags(events)

    def test_legitimate_time_gap_still_fires_under_the_view(self, monkeypatch):
        data = {"schema_version": 1, "task_boundary": {"signals": ["time_gap"], "time_gap_seconds": 60}}
        clock = [1000.0]
        monkeypatch.setattr(builder_module.time, "monotonic", lambda: clock[0])
        b, _, _ = _governed(data)
        first = _scope(b, "fs_write", "filesystem/a/b.txt", OperationType.WRITE, "t0")
        clock[0] += 1000.0
        second = _scope(b, "fs_write", "filesystem/a/c.txt", OperationType.WRITE, "t1")
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in first.advisory_flags
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in second.advisory_flags

    @pytest.mark.parametrize("depth", ["deep", 0, -3, True, 1.5])
    def test_valid_dir_change_still_crosses_under_the_substituted_depth(self, depth, caplog):
        data = {"schema_version": 1, "task_boundary": {"signals": ["dir_change"], "dir_change_depth": depth}}
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed(data)
            events = _two_dirs(b)
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in events[1].advisory_flags
        assert len(_tolerance_warnings(caplog, "task_boundary.dir_change_depth")) == 1

    def test_non_list_signals_disable_boundary_detection(self, caplog):
        data = {"schema_version": 1, "task_boundary": {"signals": "dir_change"}}
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed(data)
            events = _two_dirs(b)
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in _flags(events)
        assert len(_tolerance_warnings(caplog, "task_boundary.signals")) == 1

    def test_non_string_signal_entries_are_ignored_and_the_valid_one_applies(self, caplog):
        data = {"schema_version": 1, "task_boundary": {"signals": [1, None, "dir_change"]}}
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed(data)
            events = _two_dirs(b)
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED in events[1].advisory_flags
        assert len(_tolerance_warnings(caplog, "task_boundary.signals")) == 1


class TestSectionsAndSchemaVersion:
    @pytest.mark.parametrize("section_value", [[["demand_at", "never"]], "never", 5])
    def test_malformed_session_intent_is_not_policy(self, section_value):
        # A list of pairs would become {'demand_at': 'never'} through dict();
        # never must not be manufactured, so POL-001 stays on the write.
        data = {"schema_version": 1, "session_intent": section_value}
        b, _, _ = _governed(data)
        events = _two_dirs(b)
        assert PolicyViolation.POL_001 in events[1].policy_violations

    @pytest.mark.parametrize("section_value", [["dir_change"], "x", 3])
    def test_malformed_task_boundary_section_is_ignored(self, section_value, caplog):
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed({"schema_version": 1, "task_boundary": section_value})
            events = _two_dirs(b)
        assert AdvisoryFlag.TASK_BOUNDARY_CROSSED not in _flags(events)
        assert len(_tolerance_warnings(caplog, "task_boundary")) == 1

    @pytest.mark.parametrize("sv, recorded", [(True, None), (1.5, None), ("1", None), (1, 1), (2, 2)])
    def test_schema_version_is_recorded_raw_not_coerced(self, sv, recorded):
        _, reg, _ = _governed({"schema_version": sv})
        assert reg.payload.profile_schema_version == recorded
        assert reg.payload.profile_loaded is True


class TestToolPatterns:
    def test_non_string_entries_are_skipped_and_the_valid_pattern_still_flags(self, caplog):
        data = {"schema_version": 1, "high_consequence": {"tools": [1, None, "Bash:shell"]}}
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed(data)
            events = _two_dirs(b)
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED in events[2].advisory_flags
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED not in events[0].advisory_flags
        assert len(_tolerance_warnings(caplog, "high_consequence.tools")) == 1

    def test_non_list_tools_disable_pattern_matching(self, caplog):
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed({"schema_version": 1, "high_consequence": {"tools": "Bash:shell"}})
            events = _two_dirs(b)
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED not in _flags(events)
        assert len(_tolerance_warnings(caplog, "high_consequence.tools")) == 1

    def test_bad_regex_is_skipped_silently_at_runtime(self, caplog):
        # Warning-only at validation; the runtime skips it as it always has.
        data = {"schema_version": 1, "high_consequence": {"tools": ["(", "Bash:shell"]}}
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed(data)
            events = _two_dirs(b)
        assert AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED in events[2].advisory_flags
        assert _tolerance_warnings(caplog) == []


class TestWarningsOncePerSessionPerField:
    def test_many_events_two_malformed_fields_two_warnings(self, caplog):
        data = {
            "schema_version": 1,
            "task_boundary": {"signals": ["dir_change"], "dir_change_depth": "deep", "time_gap_seconds": "soon"},
        }
        with caplog.at_level(logging.WARNING):
            b, _, _ = _governed(data)
            for i in range(6):
                _scope(b, "fs_write", f"filesystem/d{i}/f.py", OperationType.WRITE, f"t{i}")
        assert len(_tolerance_warnings(caplog, "task_boundary.dir_change_depth")) == 1
        assert len(_tolerance_warnings(caplog, "task_boundary.time_gap_seconds")) == 1
        assert len(_tolerance_warnings(caplog)) == 2
        assert all(SID in m for m in _tolerance_warnings(caplog))

    def test_no_new_event_types_or_flags_are_introduced(self, caplog):
        data = {"schema_version": True, "session_intent": "x", "task_boundary": {"signals": 5}, "high_consequence": {"tools": [1]}}
        with caplog.at_level(logging.WARNING):
            b, reg, _ = _governed(data)
            events = _two_dirs(b)
        assert reg.event_type.value == "AGENT_REGISTERED"
        assert {e.event_type.value for e in events} == {"SCOPE_ASSERTED"}
        known = {f.value for f in AdvisoryFlag}
        assert all(getattr(f, "value", f) in known for e in events for f in e.advisory_flags)
        assert len(_tolerance_warnings(caplog)) == 4
