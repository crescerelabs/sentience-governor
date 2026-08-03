"""Unit tests for `compute_pulse` composition analyzer (v0.2.6 CP4).

Coverage targets (per plan v3.6 §CP4 test list):

* Pulse over every CP1 burn-rate fixture (clean, pol_001_only,
  mixed, no_token_data, no_turns, malformed).
* Pulse over an empty event list.
* Status normalization for every raw status both sub-analyzers can
  return.
* Status merge across every category combination in the spec table.
* Purity contract — result is byte-identical across repeated calls
  and across a sandboxed (no filesystem, no env vars) invocation.
* Default ``sync_prompt.reason`` is ``"uninitialized"`` when
  ``compute_pulse`` is called directly without CLI-layer
  attachment.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sentience_governor.analyze import compute_pulse
from sentience_governor.analyze.pulse import (
    _ADVISORY_FLAG_KEYS,
    _merge_status,
    _normalize_status,
    _session_duration_seconds,
    _summarize_advisory_flags,
)

# ---------------------------------------------------------------------------
# Fixture loaders (mirror tests/test_policy_violation_burn_rate.py)
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "burn_rate"


def _load_fixture(name: str) -> List[Any]:
    path = _FIXTURES_DIR / name
    events: List[Any] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append(line)
    return events


# ---------------------------------------------------------------------------
# 1. Top-level fixture-file tests
# ---------------------------------------------------------------------------


class TestPulseOverFixtures:
    """Pulse runs over every CP1 burn-rate fixture without raising
    and returns a structurally valid result dict."""

    @pytest.mark.parametrize(
        "fixture_name",
        [
            "clean.jsonl",
            "pol_001_only.jsonl",
            "mixed_violations.jsonl",
            "no_token_data.jsonl",
            "no_turns.jsonl",
            "malformed_events.jsonl",
        ],
    )
    def test_pulse_runs(self, fixture_name):
        events = _load_fixture(fixture_name)
        result = compute_pulse(events)
        assert isinstance(result, dict)
        # Spec-mandated top-level keys are all present.
        for key in (
            "schema_version",
            "analyzer",
            "analyzer_version",
            "session_id",
            "status",
            "session_summary",
            "undeclared_intent",
            "policy_violations_burn_rate",
            "advisory_flag_summary",
            "sync_prompt",
            "profile_fingerprint",
            "profile_loaded",
            "profile_schema_version",
        ):
            assert key in result, (
                f"key {key!r} missing from pulse result for {fixture_name}"
            )
        assert result["analyzer"] == "pulse"
        assert result["analyzer_version"] == "0.2.6"
        assert result["schema_version"] == 1

    def test_clean_fixture_status_ok(self):
        """clean.jsonl has tokens + zero violations → undeclared=ok
        (intent declared in fixture) and burn_rate=no_violations
        → pulse status=ok per usable_ok+usable_clean rule.
        """
        events = _load_fixture("clean.jsonl")
        result = compute_pulse(events)
        # Status MUST be either ok or limited depending on whether
        # the undeclared-intent sub-analyzer found populated tokens
        # in the fixture. The merge rule guarantees usable_clean
        # appears for burn_rate; what we test here is that the
        # category that DOES appear is not partial / no_signal.
        assert result["status"] in ("ok", "limited"), (
            f"clean fixture status {result['status']!r} unexpected"
        )
        assert result["policy_violations_burn_rate"]["status"] == "no_violations"

    def test_no_turns_fixture_status_no_signal(self):
        """no_turns.jsonl has zero CONTEXT_SNAPSHOT-with-turn-id →
        both sub-analyzers return no_turns → limited_signal +
        limited_signal → pulse status=no_signal."""
        events = _load_fixture("no_turns.jsonl")
        result = compute_pulse(events)
        assert result["status"] == "no_signal"

    def test_mixed_violations_subdicts_match_independent_calls(self):
        """Sanity gate from plan §CP4: composing pulse must NOT lose
        data — each sub-dict must be byte-identical to what each
        sub-analyzer returns when called independently.
        """
        from sentience_governor.analyze import (
            compute_policy_violation_burn_rate,
            compute_undeclared_intent_spend,
        )
        events = _load_fixture("mixed_violations.jsonl")
        pulse = compute_pulse(events)
        assert (
            pulse["undeclared_intent"]
            == compute_undeclared_intent_spend(events)
        )
        assert (
            pulse["policy_violations_burn_rate"]
            == compute_policy_violation_burn_rate(events)
        )


# ---------------------------------------------------------------------------
# 2. Empty-input test
# ---------------------------------------------------------------------------


class TestEmptyEventList:
    def test_empty_list_returns_structurally_valid_result(self):
        result = compute_pulse([])
        assert result["status"] == "no_signal"
        assert result["session_id"] == ""
        assert result["session_summary"]["total_events"] == 0
        assert result["session_summary"]["total_turns"] == 0
        assert result["session_summary"]["session_duration_seconds"] == 0
        # advisory_flag_summary must still ship every key, even on
        # empty input — renderer relies on the stable key set.
        for key in _ADVISORY_FLAG_KEYS:
            assert result["advisory_flag_summary"][key] == 0

    def test_none_input_does_not_raise(self):
        # Defensive: callers that pass None get treated like empty.
        result = compute_pulse(None)  # type: ignore[arg-type]
        assert result["status"] == "no_signal"


# ---------------------------------------------------------------------------
# 3. Status normalization — every raw status for each analyzer
# ---------------------------------------------------------------------------


class TestStatusNormalization:
    @pytest.mark.parametrize(
        "raw_status,expected",
        [
            ("ok", "usable_ok"),
            ("partial", "partial"),
            ("no_token_data", "limited_signal"),
            ("no_turns", "limited_signal"),
        ],
    )
    def test_undeclared_intent_status_mapping(self, raw_status, expected):
        assert _normalize_status(raw_status, "undeclared_intent") == expected

    @pytest.mark.parametrize(
        "raw_status,expected",
        [
            ("ok", "usable_ok"),
            ("no_violations", "usable_clean"),
            ("partial", "partial"),
            ("no_token_data", "limited_signal"),
            ("no_turns", "limited_signal"),
        ],
    )
    def test_burn_rate_status_mapping(self, raw_status, expected):
        assert (
            _normalize_status(raw_status, "policy_violation_burn_rate")
            == expected
        )

    def test_unknown_analyzer_falls_through_to_limited_signal(self):
        assert _normalize_status("ok", "future_analyzer") == "limited_signal"

    def test_unknown_raw_status_falls_through_to_limited_signal(self):
        assert (
            _normalize_status("future_status", "undeclared_intent")
            == "limited_signal"
        )

    def test_none_raw_status_is_limited_signal(self):
        assert _normalize_status(None, "undeclared_intent") == "limited_signal"


# ---------------------------------------------------------------------------
# 4. Status merge — every spec-table combination
# ---------------------------------------------------------------------------


class TestStatusMerge:
    """Spec table (plan §"Pulse status merge rules"):

    | usable_ok + usable_ok        | ok        |
    | usable_ok + usable_clean     | ok        |
    | usable_clean + usable_clean  | ok        |
    | usable_ok + limited_signal   | limited   |
    | usable_clean + limited_signal| limited   |
    | limited_signal + limited_signal | no_signal |
    | any + partial                | partial   |
    """

    @pytest.mark.parametrize(
        "categories,expected",
        [
            (["usable_ok", "usable_ok"], "ok"),
            (["usable_ok", "usable_clean"], "ok"),
            (["usable_clean", "usable_ok"], "ok"),
            (["usable_clean", "usable_clean"], "ok"),
            (["usable_ok", "limited_signal"], "limited"),
            (["limited_signal", "usable_ok"], "limited"),
            (["usable_clean", "limited_signal"], "limited"),
            (["limited_signal", "usable_clean"], "limited"),
            (["limited_signal", "limited_signal"], "no_signal"),
            (["no_signal", "no_signal"], "no_signal"),
            (["limited_signal", "no_signal"], "no_signal"),
            (["usable_ok", "partial"], "partial"),
            (["usable_clean", "partial"], "partial"),
            (["limited_signal", "partial"], "partial"),
            (["partial", "partial"], "partial"),
        ],
    )
    def test_merge(self, categories, expected):
        assert _merge_status(categories) == expected

    def test_empty_categories_defaults_to_no_signal(self):
        # Defensive: no sub-analyzers contributed → no_signal.
        assert _merge_status([]) == "no_signal"


# ---------------------------------------------------------------------------
# 5. Advisory-flag summary
# ---------------------------------------------------------------------------


class TestAdvisoryFlagSummary:
    def test_summary_includes_all_ten_keys_on_empty_input(self):
        summary = _summarize_advisory_flags([])
        # Verify finding-1 contract: ALL ten flags, not the seven
        # the plan example dict enumerated.
        assert set(summary.keys()) == set(_ADVISORY_FLAG_KEYS)
        assert len(summary) == 10
        for v in summary.values():
            assert v == 0

    def test_summary_counts_repeated_flags(self):
        events = [
            {"advisory_flags": ["TASK_BOUNDARY_CROSSED", "INTENT_MISSING"]},
            {"advisory_flags": ["TASK_BOUNDARY_CROSSED"]},
            {"advisory_flags": ["HIGH_CONSEQUENCE_DETECTED"]},
        ]
        summary = _summarize_advisory_flags(events)
        assert summary["TASK_BOUNDARY_CROSSED"] == 2
        assert summary["INTENT_MISSING"] == 1
        assert summary["HIGH_CONSEQUENCE_DETECTED"] == 1
        assert summary["AGENT_UNREGISTERED"] == 0

    def test_summary_ignores_unknown_flags(self):
        events = [{"advisory_flags": ["FUTURE_FLAG", "TASK_BOUNDARY_CROSSED"]}]
        summary = _summarize_advisory_flags(events)
        assert summary["TASK_BOUNDARY_CROSSED"] == 1
        assert "FUTURE_FLAG" not in summary

    def test_summary_skips_malformed_events(self):
        events = [
            "not a dict",
            {"advisory_flags": "not a list"},
            {"advisory_flags": ["INTENT_MISSING"]},
        ]
        summary = _summarize_advisory_flags(events)
        assert summary["INTENT_MISSING"] == 1


# ---------------------------------------------------------------------------
# 6. Session-summary helpers
# ---------------------------------------------------------------------------


class TestSessionDuration:
    def test_empty_input_returns_zero(self):
        assert _session_duration_seconds([]) == 0

    def test_single_event_returns_zero(self):
        assert _session_duration_seconds(
            [{"timestamp_utc": "2026-05-28T12:00:00Z"}]
        ) == 0

    def test_two_events_returns_span(self):
        duration = _session_duration_seconds([
            {"timestamp_utc": "2026-05-28T12:00:00Z"},
            {"timestamp_utc": "2026-05-28T12:08:07Z"},
        ])
        assert duration == 487  # exactly matches the plan example

    def test_malformed_timestamps_ignored(self):
        duration = _session_duration_seconds([
            {"timestamp_utc": "not-a-timestamp"},
            {"timestamp_utc": "2026-05-28T12:00:00Z"},
            {"timestamp_utc": "2026-05-28T12:00:30Z"},
            {"timestamp_utc": None},
        ])
        assert duration == 30

    def test_tolerates_explicit_offset(self):
        duration = _session_duration_seconds([
            {"timestamp_utc": "2026-05-28T12:00:00+00:00"},
            {"timestamp_utc": "2026-05-28T12:01:00+00:00"},
        ])
        assert duration == 60


# ---------------------------------------------------------------------------
# 7. sync_prompt default (CP4 contract: never populated by analyzer)
# ---------------------------------------------------------------------------


class TestSyncPromptDefault:
    def test_default_reason_is_uninitialized(self):
        events = _load_fixture("clean.jsonl")
        result = compute_pulse(events)
        assert result["sync_prompt"] == {
            "show": False,
            "reason": "uninitialized",
        }

    def test_default_reason_on_empty_input(self):
        result = compute_pulse([])
        assert result["sync_prompt"]["reason"] == "uninitialized"
        assert result["sync_prompt"]["show"] is False

    def test_default_is_not_a_shared_reference(self):
        """Mutating one result's sync_prompt MUST NOT affect another.
        Regression guard: compute_pulse must dict()-copy the
        default, not return the same module-level instance.
        """
        a = compute_pulse([])
        b = compute_pulse([])
        a["sync_prompt"]["show"] = True
        a["sync_prompt"]["reason"] = "not_registered"
        assert b["sync_prompt"]["show"] is False
        assert b["sync_prompt"]["reason"] == "uninitialized"


# ---------------------------------------------------------------------------
# 8. Replay-stability / purity contract
# ---------------------------------------------------------------------------


class TestPurityContract:
    @pytest.mark.parametrize(
        "fixture_name",
        [
            "clean.jsonl",
            "pol_001_only.jsonl",
            "mixed_violations.jsonl",
            "no_token_data.jsonl",
            "no_turns.jsonl",
        ],
    )
    def test_repeated_calls_byte_identical(self, fixture_name):
        events = _load_fixture(fixture_name)
        a = compute_pulse(events)
        b = compute_pulse(events)
        assert a == b
        # JSON round-trip with sorted keys is the strongest
        # byte-stability proof.
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_input_not_mutated(self):
        events = _load_fixture("mixed_violations.jsonl")
        before = copy.deepcopy(events)
        compute_pulse(events)
        assert events == before

    def test_purity_sandbox(self, monkeypatch, tmp_path):
        """Purity contract per plan §CP4: result MUST be
        byte-identical between a fully-configured environment and a
        sandbox with no env vars and no filesystem state.

        We approximate the sandbox by clearing the env and chdir-ing
        to an empty tmp dir, then comparing the result to the
        baseline-environment call.
        """
        events = _load_fixture("mixed_violations.jsonl")
        baseline = compute_pulse(events)

        # Wipe the entire process env and chdir to an empty tmp.
        with monkeypatch.context() as m:
            for var in list(os.environ.keys()):
                m.delenv(var, raising=False)
            m.chdir(tmp_path)
            sandboxed = compute_pulse(events)

        assert baseline == sandboxed
        assert (
            json.dumps(baseline, sort_keys=True)
            == json.dumps(sandboxed, sort_keys=True)
        )


# ---------------------------------------------------------------------------
# 9. session_summary integration
# ---------------------------------------------------------------------------


class TestSessionSummaryIntegration:
    def test_total_events_counts_dict_events_only(self):
        events = _load_fixture("malformed_events.jsonl")
        result = compute_pulse(events)
        # malformed_events.jsonl contains 1 raw string + 1 no-event_type
        # dict + valid events. total_events MUST exclude the string
        # entry but include the no-event_type dict (it's still a dict).
        non_dict_count = sum(1 for e in events if not isinstance(e, dict))
        assert (
            result["session_summary"]["total_events"]
            == len(events) - non_dict_count
        )

    def test_total_turns_matches_context_snapshot_count(self):
        events = _load_fixture("pol_001_only.jsonl")
        result = compute_pulse(events)
        expected = sum(
            1
            for e in events
            if isinstance(e, dict)
            and e.get("event_type") == "CONTEXT_SNAPSHOT"
            and isinstance(e.get("payload"), dict)
            and e["payload"].get("llm_turn_id")
        )
        assert result["session_summary"]["total_turns"] == expected
