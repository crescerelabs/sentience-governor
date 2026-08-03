"""Unit tests for `policy_violation_burn_rate` analyzer (v0.2.6 CP1).

Tests cover:
    - All six fixture files under tests/fixtures/burn_rate/
    - Empty event list
    - Single-rule firings (POL-001 only, POL-002 only, etc.)
    - Multi-rule same-turn firings (non-additivity discipline)
    - Turn-window bracketing edge cases (no_turn_id, dedupe, conflict)
    - Same-event attribution on CONTEXT_SNAPSHOT (the v3.6 finding)
    - Inter-event buffered attribution (AGENT_REGISTERED, SCOPE_ASSERTED,
      MEMORY_WRITE_ATTEMPT)
    - Coexisting same-event + inter-event attribution on a single turn
    - Replay stability (byte-identical dict on repeated calls)
    - Pure-function discipline (no fs, no env, no input mutation)
    - Warning counter coverage (malformed, unknown_rule, untokened_pair,
      dedupe_conflict, unpaired_violation)

Coverage target: >85% of policy_violation_burn_rate.py. Matches v0.2.4
test standard.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sentience_governor.analyze.policy_violation_burn_rate import (
    compute_policy_violation_burn_rate,
    _POL_DESCRIPTIONS,
    _KNOWN_POL_RULES,
    _SAMPLE_TURN_LIMIT,
    _SCHEMA_VERSION,
    _ANALYZER_NAME,
    _ANALYZER_VERSION,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_NO_VIOLATIONS,
    STATUS_NO_TOKEN_DATA,
    STATUS_NO_TURNS,
)


# ---------------------------------------------------------------------------
# Fixture loaders
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "burn_rate"


def _load_fixture(name: str) -> List[Any]:
    """Load a fixture JSONL file. Malformed lines are kept as-is so that
    the malformed_events fixture exercises walker robustness rather than
    JSON-parser robustness.
    """
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
# Top-level fixture-file tests
# ---------------------------------------------------------------------------


class TestCleanFixture:
    def test_status_no_violations(self):
        events = _load_fixture("clean.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_NO_VIOLATIONS

    def test_zero_violations(self):
        events = _load_fixture("clean.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["violation_firing_turns"] == 0
        assert r["violation_associated_tokens"] == 0
        assert r["by_rule"] == {}

    def test_tokens_aggregated(self):
        events = _load_fixture("clean.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["total_tokens"] > 0  # 2 turns, each ~1440 tokens

    def test_session_id_captured(self):
        events = _load_fixture("clean.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["session_id"] == "fixture-burn-rate-clean-001"

    def test_profile_metadata_captured(self):
        events = _load_fixture("clean.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["profile_loaded"] is True
        assert r["profile_schema_version"] == 1
        assert r["profile_fingerprint"] == "fixt000clean"

    def test_no_notes_when_no_violations(self):
        events = _load_fixture("clean.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["notes"] == []
        assert r["notes_short"] == []


class TestPol001OnlyFixture:
    def test_status_ok(self):
        events = _load_fixture("pol_001_only.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_OK

    def test_only_pol_001(self):
        events = _load_fixture("pol_001_only.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert list(r["by_rule"].keys()) == ["POL-001"]
        assert r["by_rule"]["POL-001"]["turn_count"] == 3

    def test_no_non_additivity_note_with_single_rule(self):
        events = _load_fixture("pol_001_only.jsonl")
        r = compute_policy_violation_burn_rate(events)
        # Only one rule with non-zero token_cost → no non-additivity note.
        assert r["notes"] == []
        assert r["notes_short"] == []

    def test_all_three_turns_have_pol_001(self):
        events = _load_fixture("pol_001_only.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["violation_firing_turns"] == 3
        # All 3 turn_ids should appear as samples (limit is 3).
        samples = r["by_rule"]["POL-001"]["sample_turn_ids"]
        assert samples == ["turn-1", "turn-2", "turn-3"]


class TestMixedViolationsFixture:
    """The critical CP1 fixture — both attribution paths exercised, all
    five POL rules across the four emitting event types."""

    def test_status_ok(self):
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_OK

    def test_all_five_pol_rules_captured(self):
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert set(r["by_rule"].keys()) == {
            "POL-001",
            "POL-002",
            "POL-003",
            "POL-004",
            "POL-005",
        }

    def test_inter_event_buffered_attribution_to_turn_1(self):
        """POL-002 (AGENT_REGISTERED) and POL-001 (SCOPE_ASSERTED) buffer
        until the first CONTEXT_SNAPSHOT with llm_turn_id, then attribute
        to turn-1."""
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["by_rule"]["POL-001"]["sample_turn_ids"] == ["turn-1"]
        assert r["by_rule"]["POL-002"]["sample_turn_ids"] == ["turn-1"]

    def test_same_event_attribution_pol_003_to_turn_2(self):
        """POL-003 carried on the CONTEXT_SNAPSHOT establishing turn-2
        attributes to turn-2 (intra-event)."""
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["by_rule"]["POL-003"]["sample_turn_ids"] == ["turn-2"]

    def test_same_event_and_buffered_coexist_on_turn_3(self):
        """POL-004 (MEMORY_WRITE_ATTEMPT, buffered) and POL-005
        (CONTEXT_SNAPSHOT, same-event) both attribute to turn-3."""
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["by_rule"]["POL-004"]["sample_turn_ids"] == ["turn-3"]
        assert r["by_rule"]["POL-005"]["sample_turn_ids"] == ["turn-3"]

    def test_violation_firing_turns_dedupe(self):
        """All 3 turns have at least one violation → 3 firing turns.
        Critical: NOT 5 (which would double-count turns with multiple
        rules)."""
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["violation_firing_turns"] == 3

    def test_violation_associated_tokens_dedupes_by_turn(self):
        """violation_associated_tokens should equal sum of token-totals
        across the 3 unique violation-firing turns, NOT the sum of
        by_rule.X.token_cost (which double-counts)."""
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["violation_associated_tokens"] == r["total_tokens"]
        # Sanity: sum of by-rule tokens exceeds the deduped total when
        # multiple rules fire on the same turn.
        per_rule_sum = sum(slot["token_cost"] for slot in r["by_rule"].values())
        assert per_rule_sum > r["violation_associated_tokens"]

    def test_non_additivity_note_present(self):
        """With 5 rules each carrying non-zero token_cost, the
        non-additivity note MUST appear in both notes and notes_short."""
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert len(r["notes"]) == 1
        assert len(r["notes_short"]) == 1
        assert "not additive" in r["notes"][0]
        assert "not additive" in r["notes_short"][0]

    def test_pol_descriptions_populated(self):
        events = _load_fixture("mixed_violations.jsonl")
        r = compute_policy_violation_burn_rate(events)
        for rule_str in _KNOWN_POL_RULES:
            assert r["by_rule"][rule_str]["description"] == _POL_DESCRIPTIONS[rule_str]


class TestNoTokenDataFixture:
    def test_status_no_token_data(self):
        events = _load_fixture("no_token_data.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_NO_TOKEN_DATA

    def test_total_tokens_zero(self):
        events = _load_fixture("no_token_data.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["total_tokens"] == 0

    def test_violations_still_recorded_with_zero_token_cost(self):
        """Even when status is no_token_data, the walker preserves
        which rules fired and on which turns — operators want this
        information even when token attribution is missing. Each rule's
        token_cost is 0 because no tokens were populated.
        """
        events = _load_fixture("no_token_data.jsonl")
        r = compute_policy_violation_burn_rate(events)
        # POL-001 fired on the two SCOPE_ASSERTED events, attributed to
        # the two turn-establishing CONTEXT_SNAPSHOTs.
        assert "POL-001" in r["by_rule"]
        assert r["by_rule"]["POL-001"]["turn_count"] == 2
        # Token cost is zero because the fixture deliberately omits
        # populated tokens (status=no_token_data).
        assert r["by_rule"]["POL-001"]["token_cost"] == 0


class TestNoTurnsFixture:
    def test_status_no_turns(self):
        events = _load_fixture("no_turns.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_NO_TURNS

    def test_by_rule_empty(self):
        events = _load_fixture("no_turns.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["by_rule"] == {}

    def test_violation_metrics_zero(self):
        events = _load_fixture("no_turns.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["violation_firing_turns"] == 0
        assert r["violation_associated_tokens"] == 0
        assert r["total_tokens"] == 0

    def test_unpaired_by_rule_reports_measured_counts(self):
        """FIX-4 (v0.2.8): a turn-less session still reports WHICH rules
        fired — the counts are measured; only token attribution is
        deferred. (Closes the vacuum Claude filled by reading raw JSONL
        in the v0.2.7.1 live clean-room.)"""
        events = _load_fixture("no_turns.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["unpaired_by_rule"] == {"POL-001": 2}
        # Consistent with the scalar that already existed.
        assert sum(r["unpaired_by_rule"].values()) == r[
            "unpaired_violation_count"
        ]

    def test_unpaired_by_rule_empty_on_clean_fixture(self):
        """Additive-field sanity: clean sessions carry an empty dict."""
        events = _load_fixture("clean.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["unpaired_by_rule"] == {}


class TestMalformedEventsFixture:
    def test_status_partial(self):
        events = _load_fixture("malformed_events.jsonl")
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_PARTIAL

    def test_malformed_count_nonzero(self):
        events = _load_fixture("malformed_events.jsonl")
        r = compute_policy_violation_burn_rate(events)
        # 1 not-dict line + 1 no-event_type line = 2 malformed events.
        assert r["malformed_event_count"] == 2

    def test_warnings_populated(self):
        events = _load_fixture("malformed_events.jsonl")
        r = compute_policy_violation_burn_rate(events)
        warning_codes = {w["code"] for w in r["warnings"]}
        assert "malformed_event" in warning_codes

    def test_valid_events_still_processed(self):
        events = _load_fixture("malformed_events.jsonl")
        r = compute_policy_violation_burn_rate(events)
        # Two valid SCOPE_ASSERTED events with POL-001 + two valid
        # CONTEXT_SNAPSHOTs → 2 turns each with POL-001.
        assert "POL-001" in r["by_rule"]
        assert r["by_rule"]["POL-001"]["turn_count"] == 2


# ---------------------------------------------------------------------------
# Edge-case tests (synthesized inline, not from fixtures)
# ---------------------------------------------------------------------------


class TestEmptyAndDegenerateInputs:
    def test_empty_event_list(self):
        r = compute_policy_violation_burn_rate([])
        assert r["status"] == STATUS_NO_TURNS
        assert r["total_tokens"] == 0
        assert r["session_id"] == ""
        assert r["by_rule"] == {}

    def test_none_input(self):
        r = compute_policy_violation_burn_rate(None)  # type: ignore[arg-type]
        assert r["status"] == STATUS_NO_TURNS
        assert r["total_tokens"] == 0

    def test_all_malformed_input(self):
        events = ["not a dict", 42, None, [], {"no_event_type": True}]
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_NO_TURNS
        assert r["malformed_event_count"] >= 4  # not-dict + no-event_type


class TestSingleRuleFirings:
    """Each rule firing on its own event type, attribution verified."""

    def _make_session_with_violation(
        self,
        event_type: str,
        primitive: str,
        rule: str,
        payload: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Construct a minimal session that fires a single POL rule on
        the given event type, followed by a turn-establishing
        CONTEXT_SNAPSHOT for attribution."""
        return [
            {
                "event_id": "evt-trigger",
                "event_type": event_type,
                "session_id": "synth-single-rule",
                "event_sequence_number": 1,
                "agent_id": "synth-agent",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:00.000Z",
                "primitive": primitive,
                "payload": payload,
                "advisory_flags": [],
                "policy_violations": [rule],
                "simulated_consequence": None,
                "pass_through": True,
            },
            {
                "event_id": "evt-ctx",
                "event_type": "CONTEXT_SNAPSHOT",
                "session_id": "synth-single-rule",
                "event_sequence_number": 2,
                "agent_id": "synth-agent",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:01.000Z",
                "primitive": "CONTEXT",
                "payload": {
                    "data_classifications": ["internal"],
                    "classification_source": "explicit",
                    "provenance": [],
                    "retention_flags": [],
                    "context_size_tokens": 500,
                    "llm_prompt_tokens": 500,
                    "llm_completion_tokens": 100,
                    "llm_turn_id": "turn-1",
                },
                "advisory_flags": [],
                "policy_violations": [],
                "simulated_consequence": None,
                "pass_through": True,
            },
        ]

    def test_pol_002_on_agent_registered_buffered_to_turn(self):
        events = self._make_session_with_violation(
            "AGENT_REGISTERED",
            "REGISTRATION",
            "POL-002",
            {
                "agent_id": "synth-agent",
                "agent_version": "1.0",
                "vendor_id": "synth",
                "deployment_mode": "vendor_managed",
                "declared_capabilities": [],
                "owner_claim": "op@x.com",
            },
        )
        r = compute_policy_violation_burn_rate(events)
        assert "POL-002" in r["by_rule"]
        assert r["by_rule"]["POL-002"]["sample_turn_ids"] == ["turn-1"]

    def test_pol_001_on_scope_asserted_buffered_to_turn(self):
        events = self._make_session_with_violation(
            "SCOPE_ASSERTED",
            "SCOPE",
            "POL-001",
            {
                "tool_id": "fs.write",
                "asserted_permissions": ["write"],
                "target_system": "f.txt",
                "operation_type": "WRITE",
            },
        )
        r = compute_policy_violation_burn_rate(events)
        assert "POL-001" in r["by_rule"]
        assert r["by_rule"]["POL-001"]["sample_turn_ids"] == ["turn-1"]

    def test_pol_004_on_memory_write_attempt_buffered_to_turn(self):
        events = self._make_session_with_violation(
            "MEMORY_WRITE_ATTEMPT",
            "MEMORY",
            "POL-004",
            {
                "write_type": "explicit_persist",
                "target_store": "session.memory",
                "write_classification": "unclassified",
                "write_size_tokens": 100,
            },
        )
        r = compute_policy_violation_burn_rate(events)
        assert "POL-004" in r["by_rule"]
        assert r["by_rule"]["POL-004"]["sample_turn_ids"] == ["turn-1"]


class TestSameEventAttributionOnContextSnapshot:
    """The v3.6 finding's intra-event rule: CONTEXT_SNAPSHOT carrying
    both llm_turn_id AND policy_violations attributes those violations
    to that same turn."""

    def _ctx_with_violation_and_turn(self, rule: str) -> Dict[str, Any]:
        return {
            "event_id": "evt-ctx-same",
            "event_type": "CONTEXT_SNAPSHOT",
            "session_id": "synth-same-event",
            "event_sequence_number": 1,
            "agent_id": "synth-agent",
            "deployment_mode": "vendor_managed",
            "timestamp_utc": "2026-05-28T00:00:00.000Z",
            "primitive": "CONTEXT",
            "payload": {
                "data_classifications": ["internal"],
                "classification_source": "explicit",
                "provenance": [],
                "retention_flags": [],
                "context_size_tokens": 1000,
                "llm_prompt_tokens": 1000,
                "llm_completion_tokens": 200,
                "llm_turn_id": "turn-1",
            },
            "advisory_flags": [],
            "policy_violations": [rule],
            "simulated_consequence": None,
            "pass_through": True,
        }

    def test_pol_003_same_event_attribution(self):
        events = [self._ctx_with_violation_and_turn("POL-003")]
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_OK
        assert r["by_rule"]["POL-003"]["sample_turn_ids"] == ["turn-1"]
        assert r["violation_firing_turns"] == 1

    def test_pol_005_same_event_attribution(self):
        events = [self._ctx_with_violation_and_turn("POL-005")]
        r = compute_policy_violation_burn_rate(events)
        assert r["status"] == STATUS_OK
        assert r["by_rule"]["POL-005"]["sample_turn_ids"] == ["turn-1"]


class TestTurnWindowEdgeCases:
    def test_context_snapshot_without_turn_id_is_transparent(self):
        """A CONTEXT_SNAPSHOT lacking llm_turn_id does not establish a
        turn; any policy_violations on it buffer for the next
        turn-establishing snapshot."""
        events = [
            {
                "event_id": "evt-ctx-nopw",
                "event_type": "CONTEXT_SNAPSHOT",
                "session_id": "synth-noturn",
                "event_sequence_number": 1,
                "agent_id": "synth-agent",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:00.000Z",
                "primitive": "CONTEXT",
                "payload": {
                    "data_classifications": ["internal"],
                    "classification_source": "explicit",
                    "provenance": [],
                    "retention_flags": [],
                    "context_size_tokens": 500,
                    # no llm_turn_id
                },
                "advisory_flags": [],
                "policy_violations": ["POL-003"],
                "simulated_consequence": None,
                "pass_through": True,
            },
            {
                "event_id": "evt-ctx-real",
                "event_type": "CONTEXT_SNAPSHOT",
                "session_id": "synth-noturn",
                "event_sequence_number": 2,
                "agent_id": "synth-agent",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:01.000Z",
                "primitive": "CONTEXT",
                "payload": {
                    "data_classifications": ["internal"],
                    "classification_source": "explicit",
                    "provenance": [],
                    "retention_flags": [],
                    "context_size_tokens": 600,
                    "llm_prompt_tokens": 600,
                    "llm_completion_tokens": 120,
                    "llm_turn_id": "turn-1",
                },
                "advisory_flags": [],
                "policy_violations": [],
                "simulated_consequence": None,
                "pass_through": True,
            },
        ]
        r = compute_policy_violation_burn_rate(events)
        # POL-003 from first snapshot buffered → attributes to turn-1.
        assert r["by_rule"]["POL-003"]["sample_turn_ids"] == ["turn-1"]

    def test_untokened_pair_warning(self):
        """CONTEXT_SNAPSHOT with populated tokens but no llm_turn_id
        triggers an untokened_pair warning."""
        events = [
            {
                "event_id": "evt-bad",
                "event_type": "CONTEXT_SNAPSHOT",
                "session_id": "synth",
                "event_sequence_number": 1,
                "agent_id": "x",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:00.000Z",
                "primitive": "CONTEXT",
                "payload": {
                    "data_classifications": [],
                    "classification_source": "explicit",
                    "provenance": [],
                    "retention_flags": [],
                    "context_size_tokens": 500,
                    "llm_prompt_tokens": 500,
                    # missing llm_turn_id
                },
                "advisory_flags": [],
                "policy_violations": [],
                "simulated_consequence": None,
                "pass_through": True,
            },
        ]
        r = compute_policy_violation_burn_rate(events)
        assert r["untokened_pair_count"] == 1
        codes = [w["code"] for w in r["warnings"]]
        assert "untokened_pair" in codes

    def test_dedupe_conflict_warning(self):
        """Two CONTEXT_SNAPSHOTs with the same llm_turn_id and
        conflicting token values raise a dedupe_conflict warning."""
        events = [
            {
                "event_id": "evt-a",
                "event_type": "CONTEXT_SNAPSHOT",
                "session_id": "synth",
                "event_sequence_number": 1,
                "agent_id": "x",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:00.000Z",
                "primitive": "CONTEXT",
                "payload": {
                    "data_classifications": [],
                    "classification_source": "explicit",
                    "provenance": [],
                    "retention_flags": [],
                    "context_size_tokens": 500,
                    "llm_prompt_tokens": 500,
                    "llm_completion_tokens": 100,
                    "llm_turn_id": "turn-1",
                },
                "advisory_flags": [],
                "policy_violations": [],
                "simulated_consequence": None,
                "pass_through": True,
            },
            {
                "event_id": "evt-b",
                "event_type": "CONTEXT_SNAPSHOT",
                "session_id": "synth",
                "event_sequence_number": 2,
                "agent_id": "x",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:01.000Z",
                "primitive": "CONTEXT",
                "payload": {
                    "data_classifications": [],
                    "classification_source": "explicit",
                    "provenance": [],
                    "retention_flags": [],
                    "context_size_tokens": 999,
                    "llm_prompt_tokens": 999,  # conflicts with first
                    "llm_completion_tokens": 200,
                    "llm_turn_id": "turn-1",
                },
                "advisory_flags": [],
                "policy_violations": [],
                "simulated_consequence": None,
                "pass_through": True,
            },
        ]
        r = compute_policy_violation_burn_rate(events)
        assert r["dedupe_conflict_count"] == 1
        # First-populated-wins; tokens stay at 500+100=600.
        assert r["total_tokens"] == 600

    def test_unpaired_violation_warning(self):
        """A violation on a non-CONTEXT event that's NEVER followed by
        a turn-establishing CONTEXT_SNAPSHOT generates an
        unpaired_violation warning."""
        events = [
            {
                "event_id": "evt-orphan",
                "event_type": "SCOPE_ASSERTED",
                "session_id": "synth",
                "event_sequence_number": 1,
                "agent_id": "x",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:00.000Z",
                "primitive": "SCOPE",
                "payload": {
                    "tool_id": "fs.write",
                    "asserted_permissions": ["write"],
                    "target_system": "f.txt",
                    "operation_type": "WRITE",
                },
                "advisory_flags": [],
                "policy_violations": ["POL-001"],
                "simulated_consequence": None,
                "pass_through": True,
            },
        ]
        r = compute_policy_violation_burn_rate(events)
        assert r["unpaired_violation_count"] == 1
        codes = [w["code"] for w in r["warnings"]]
        assert "unpaired_violation" in codes
        # No turn was established, so status is no_turns.
        assert r["status"] == STATUS_NO_TURNS


class TestUnknownRuleHandling:
    def test_unknown_rule_warned_not_aggregated(self):
        """An envelope containing a non-standard POL string surfaces a
        warning but is NOT added to by_rule."""
        events = [
            {
                "event_id": "evt-x",
                "event_type": "SCOPE_ASSERTED",
                "session_id": "synth",
                "event_sequence_number": 1,
                "agent_id": "x",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:00.000Z",
                "primitive": "SCOPE",
                "payload": {
                    "tool_id": "fs.write",
                    "asserted_permissions": ["write"],
                    "target_system": "f.txt",
                    "operation_type": "WRITE",
                },
                "advisory_flags": [],
                "policy_violations": ["POL-999"],  # unknown
                "simulated_consequence": None,
                "pass_through": True,
            },
            {
                "event_id": "evt-ctx",
                "event_type": "CONTEXT_SNAPSHOT",
                "session_id": "synth",
                "event_sequence_number": 2,
                "agent_id": "x",
                "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-05-28T00:00:01.000Z",
                "primitive": "CONTEXT",
                "payload": {
                    "data_classifications": [],
                    "classification_source": "explicit",
                    "provenance": [],
                    "retention_flags": [],
                    "context_size_tokens": 100,
                    "llm_prompt_tokens": 100,
                    "llm_completion_tokens": 20,
                    "llm_turn_id": "turn-1",
                },
                "advisory_flags": [],
                "policy_violations": [],
                "simulated_consequence": None,
                "pass_through": True,
            },
        ]
        r = compute_policy_violation_burn_rate(events)
        assert r["unknown_rule_count"] == 1
        assert "POL-999" not in r["by_rule"]
        codes = [w["code"] for w in r["warnings"]]
        assert "unknown_rule" in codes


# ---------------------------------------------------------------------------
# Replay stability + purity discipline
# ---------------------------------------------------------------------------


class TestReplayStability:
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
    def test_byte_identical_dict_on_repeated_calls(self, fixture_name):
        events = _load_fixture(fixture_name)
        r1 = compute_policy_violation_burn_rate(events)
        r2 = compute_policy_violation_burn_rate(events)
        r3 = compute_policy_violation_burn_rate(events)
        assert repr(r1) == repr(r2) == repr(r3)

    def test_input_not_mutated(self):
        """The analyzer must not mutate the input event list or any
        event dicts within it."""
        events = _load_fixture("mixed_violations.jsonl")
        snapshot = copy.deepcopy(events)
        compute_policy_violation_burn_rate(events)
        assert events == snapshot


class TestSchemaShapeConstants:
    """Output schema must include the four version/identity stamps."""

    def test_schema_version_present(self):
        r = compute_policy_violation_burn_rate([])
        assert r["schema_version"] == _SCHEMA_VERSION

    def test_analyzer_name_present(self):
        r = compute_policy_violation_burn_rate([])
        assert r["analyzer"] == _ANALYZER_NAME

    def test_analyzer_version_present(self):
        r = compute_policy_violation_burn_rate([])
        assert r["analyzer_version"] == _ANALYZER_VERSION


class TestSampleTurnLimit:
    def test_sample_turn_ids_bounded(self):
        """sample_turn_ids should be capped at _SAMPLE_TURN_LIMIT entries
        even for rules firing on many turns."""
        events: List[Dict[str, Any]] = []
        seq = 0
        for i in range(_SAMPLE_TURN_LIMIT + 5):  # 5 more than the limit
            seq += 1
            events.append(
                {
                    "event_id": f"evt-scope-{i}",
                    "event_type": "SCOPE_ASSERTED",
                    "session_id": "synth-many",
                    "event_sequence_number": seq,
                    "agent_id": "x",
                    "deployment_mode": "vendor_managed",
                    "timestamp_utc": f"2026-05-28T00:00:{seq:02d}.000Z",
                    "primitive": "SCOPE",
                    "payload": {
                        "tool_id": "fs.write",
                        "asserted_permissions": ["write"],
                        "target_system": "f.txt",
                        "operation_type": "WRITE",
                    },
                    "advisory_flags": [],
                    "policy_violations": ["POL-001"],
                    "simulated_consequence": None,
                    "pass_through": True,
                }
            )
            seq += 1
            events.append(
                {
                    "event_id": f"evt-ctx-{i}",
                    "event_type": "CONTEXT_SNAPSHOT",
                    "session_id": "synth-many",
                    "event_sequence_number": seq,
                    "agent_id": "x",
                    "deployment_mode": "vendor_managed",
                    "timestamp_utc": f"2026-05-28T00:00:{seq:02d}.000Z",
                    "primitive": "CONTEXT",
                    "payload": {
                        "data_classifications": [],
                        "classification_source": "explicit",
                        "provenance": [],
                        "retention_flags": [],
                        "context_size_tokens": 100,
                        "llm_prompt_tokens": 100,
                        "llm_completion_tokens": 20,
                        "llm_turn_id": f"turn-{i}",
                    },
                    "advisory_flags": [],
                    "policy_violations": [],
                    "simulated_consequence": None,
                    "pass_through": True,
                }
            )

        r = compute_policy_violation_burn_rate(events)
        assert r["by_rule"]["POL-001"]["turn_count"] == _SAMPLE_TURN_LIMIT + 5
        assert len(r["by_rule"]["POL-001"]["sample_turn_ids"]) == _SAMPLE_TURN_LIMIT
