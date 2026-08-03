"""Tests for the undeclared-intent token spend analyzer (v0.2.4).

First-cut test suite for Checkpoint 1: covers the happy path, the
load-bearing edge cases (no_token_data, no_turns, surface-bound
sessions), the conservative-marking rule (any undeclared call in a
turn marks the whole turn), and the pure-function guarantees
(byte-stable output, no input mutation).

Additional edge-case coverage (warning counters, malformed traces,
performance smoke test) lands in Checkpoint 2.
"""

from __future__ import annotations

import copy

from sentience_governor.analyze import compute_undeclared_intent_spend
from sentience_governor.analyze.undeclared_intent import (
    STATUS_NO_TOKEN_DATA,
    STATUS_NO_TURNS,
    STATUS_OK,
    STATUS_PARTIAL,
)


# ---------------------------------------------------------------------------
# Synthetic event builders — small helpers to keep test fixtures readable.
# ---------------------------------------------------------------------------


def _make_intent_declared(stated_objective: str = "Generate Q1 report"):
    return {
        "event_type": "INTENT_DECLARED",
        "session_id": "test-session",
        "event_sequence_number": 2,
        "agent_id": "test-agent",
        "payload": {
            "stated_objective": stated_objective,
            "intent_source": "explicit",
            "intent_confidence": "explicit",
            "session_scope_hint": [],
        },
        "advisory_flags": [],
        "policy_violations": [],
    }


def _make_scope_asserted(
    tool_id: str = "crm.read",
    *,
    advisory_flags=None,
    policy_violations=None,
    seq: int = 3,
    operation_type: str = "READ",
    tool_use_id: str | None = None,
):
    payload = {
        "tool_id": tool_id,
        "asserted_permissions": ["read"],
        "target_system": "crm",
        "operation_type": operation_type,
    }
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    return {
        "event_type": "SCOPE_ASSERTED",
        "session_id": "test-session",
        "event_sequence_number": seq,
        "agent_id": "test-agent",
        "payload": payload,
        "advisory_flags": list(advisory_flags) if advisory_flags else [],
        "policy_violations": list(policy_violations) if policy_violations else [],
    }


def _make_context_snapshot(
    *,
    llm_turn_id: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    seq: int = 4,
    tool_use_ids: list | None = None,
):
    payload = {
        "data_classifications": [],
        "classification_source": "unclassified",
        "provenance": [],
        "retention_flags": [],
        "context_size_tokens": 100,
    }
    if llm_turn_id is not None:
        payload["llm_turn_id"] = llm_turn_id
    if tool_use_ids is not None:
        payload["tool_use_ids"] = list(tool_use_ids)
    if prompt_tokens is not None:
        payload["llm_prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        payload["llm_completion_tokens"] = completion_tokens
    return {
        "event_type": "CONTEXT_SNAPSHOT",
        "session_id": "test-session",
        "event_sequence_number": seq,
        "agent_id": "test-agent",
        "payload": payload,
        "advisory_flags": [],
        "policy_violations": [],
    }


# ---------------------------------------------------------------------------
# Happy path — golden-trace test
# ---------------------------------------------------------------------------


def test_happy_path_single_declared_turn():
    """One LLM turn with declared intent and no INTENT_MISSING/POL-001 →
    undeclared_ratio = 0.0, declared_tokens = total_tokens."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.read", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, completion_tokens=50, seq=4
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["status"] == STATUS_OK
    assert result["session_has_declared_intent"] is True
    assert result["total_tokens"] == 150
    assert result["undeclared_tokens"] == 0
    assert result["declared_tokens"] == 150
    assert result["undeclared_ratio"] == 0.0
    assert result["undeclared_percent"] == 0.0
    assert result["total_turn_count"] == 1
    assert result["undeclared_turn_count"] == 0
    assert result["undeclared_turns"] == []


def test_single_undeclared_turn_via_intent_missing():
    """SCOPE_ASSERTED with INTENT_MISSING in advisory_flags marks the
    next turn-establishing CONTEXT_SNAPSHOT's turn as undeclared."""
    events = [
        _make_scope_asserted(
            "crm.write_snapshot",
            advisory_flags=["INTENT_MISSING"],
            seq=2,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=200, completion_tokens=100, seq=3
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["status"] in (STATUS_OK, STATUS_PARTIAL)
    assert result["session_has_declared_intent"] is False
    assert result["total_tokens"] == 300
    assert result["undeclared_tokens"] == 300
    assert result["declared_tokens"] == 0
    assert result["undeclared_ratio"] == 1.0
    assert result["undeclared_percent"] == 100.0
    assert result["undeclared_turn_count"] == 1
    assert len(result["undeclared_turns"]) == 1
    assert result["undeclared_turns"][0]["turn_id"] == "turn-1"
    assert result["undeclared_turns"][0]["tokens"] == 300
    assert "INTENT_MISSING" in result["undeclared_turns"][0]["reasons"]
    assert "crm.write_snapshot" in result["undeclared_turns"][0]["tool_ids"]


def test_single_undeclared_turn_via_pol_001():
    """SCOPE_ASSERTED with POL-001 in policy_violations marks the next
    turn-establishing CONTEXT_SNAPSHOT's turn as undeclared (separate
    code path from INTENT_MISSING)."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted(
            "postgres.query",
            policy_violations=["POL-001"],
            seq=3,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=420, completion_tokens=180, seq=4
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["session_has_declared_intent"] is True
    assert result["undeclared_tokens"] == 600
    assert result["undeclared_turn_count"] == 1
    assert "POL-001" in result["undeclared_turns"][0]["reasons"]


def test_conservative_marking_rule_mixed_calls():
    """Turn with one undeclared and one declared SCOPE_ASSERTED → whole
    turn marked undeclared (conservative rule per plan v3)."""
    events = [
        _make_intent_declared(),
        # First SA — declared (no flags)
        _make_scope_asserted("crm.read", seq=3),
        # Second SA — undeclared (POL-001)
        _make_scope_asserted(
            "postgres.query",
            policy_violations=["POL-001"],
            seq=4,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=500, completion_tokens=250, seq=5
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["undeclared_turn_count"] == 1
    assert result["undeclared_tokens"] == 750
    assert result["declared_tokens"] == 0
    # Both tool_ids should appear in the reasons list (or only the
    # offending one — implementation includes only flag-bearing tools).
    assert "postgres.query" in result["undeclared_turns"][0]["tool_ids"]


def test_no_token_data_status():
    """Session with no CONTEXT_SNAPSHOTs carrying llm_turn_id → status
    no_token_data; surface-bound case (the most common Claude Code shape
    today)."""
    events = [
        _make_scope_asserted("crm.read", seq=2),
        _make_context_snapshot(seq=3),  # no llm_turn_id
        _make_scope_asserted("crm.write", seq=4),
        _make_context_snapshot(seq=5),  # no llm_turn_id
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["status"] == STATUS_NO_TOKEN_DATA
    assert result["total_turn_count"] == 0
    assert result["total_tokens"] == 0
    assert result["undeclared_tokens"] == 0
    assert result["undeclared_ratio"] == 0.0
    # session_has_declared_intent reflects whether INTENT_DECLARED was
    # seen, regardless of token data.
    assert result["session_has_declared_intent"] is False


def test_no_turns_status_when_turn_ids_present_but_no_tokens():
    """CONTEXT_SNAPSHOTs with llm_turn_id but no populated token fields
    → status no_turns. This is the rare case where Track 2 hooks were
    wired but no LLM calls produced token data."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.read", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", seq=4
        ),  # turn_id but no tokens
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["status"] == STATUS_NO_TURNS
    assert result["total_turn_count"] == 1
    assert result["total_tokens"] == 0


def test_multi_turn_mixed_declared_undeclared():
    """Two turns: one declared, one undeclared (POL-001) → ratio is
    proportional."""
    events = [
        _make_intent_declared(),
        # Turn 1 — declared
        _make_scope_asserted("crm.read", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, completion_tokens=50, seq=4
        ),
        # Turn 2 — undeclared via POL-001
        _make_scope_asserted(
            "postgres.query",
            policy_violations=["POL-001"],
            seq=5,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-2", prompt_tokens=400, completion_tokens=200, seq=6
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["total_turn_count"] == 2
    assert result["undeclared_turn_count"] == 1
    assert result["total_tokens"] == 750
    assert result["declared_tokens"] == 150
    assert result["undeclared_tokens"] == 600
    # 600 / 750 = 0.8 = 80%
    assert result["undeclared_ratio"] == 0.8
    assert result["undeclared_percent"] == 80.0


def test_multi_tool_calls_per_turn_dedupe_correctly():
    """One LLM turn with three tool calls → three CONTEXT_SNAPSHOTs
    sharing the same llm_turn_id and the same token data → tokens
    counted once per turn (dedupe precedence — first populated wins).

    This is the v0.2.3 attribution contract enforced by the analyzer."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.get_customer", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=4812, completion_tokens=932, seq=4
        ),
        _make_scope_asserted("crm.list_invoices", seq=5),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=4812, completion_tokens=932, seq=6
        ),
        _make_scope_asserted("crm.write_snapshot", seq=7),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=4812, completion_tokens=932, seq=8
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    # Turn 1 has tokens 4812 + 932 = 5744. Counted ONCE despite three
    # CONTEXT_SNAPSHOTs sharing the same llm_turn_id.
    assert result["total_turn_count"] == 1
    assert result["total_tokens"] == 5744
    # No naive 3x-inflation (which would be 17,232).


def test_session_has_declared_intent_false_when_objective_empty():
    """INTENT_DECLARED with an empty stated_objective should NOT flip
    session_has_declared_intent to True — empty / placeholder
    declarations don't count as the surface honestly declaring intent."""
    events = [
        _make_intent_declared(stated_objective=""),
        _make_intent_declared(stated_objective="   "),  # whitespace only
        _make_scope_asserted("crm.read", seq=4),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, seq=5
        ),
    ]
    result = compute_undeclared_intent_spend(events)
    assert result["session_has_declared_intent"] is False


def test_pure_function_does_not_mutate_input():
    """Calling compute_undeclared_intent_spend must not mutate the
    input event list or any event dict within it."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.read", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, completion_tokens=50, seq=4
        ),
    ]
    snapshot_before = copy.deepcopy(events)

    compute_undeclared_intent_spend(events)

    assert events == snapshot_before, "input events were mutated"


def test_pure_function_byte_stable_output():
    """Two consecutive calls with identical input must produce
    repr-equal output (byte-stable guarantee)."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted(
            "postgres.query",
            policy_violations=["POL-001"],
            seq=3,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=200, completion_tokens=100, seq=4
        ),
    ]

    result_a = compute_undeclared_intent_spend(events)
    result_b = compute_undeclared_intent_spend(events)

    assert repr(result_a) == repr(result_b)


def test_empty_event_list():
    """Empty input → no_token_data status, all numeric fields zero."""
    result = compute_undeclared_intent_spend([])

    assert result["status"] == STATUS_NO_TOKEN_DATA
    assert result["total_tokens"] == 0
    assert result["undeclared_tokens"] == 0
    assert result["declared_tokens"] == 0
    assert result["total_turn_count"] == 0
    assert result["undeclared_turn_count"] == 0
    assert result["undeclared_turns"] == []
    assert result["session_has_declared_intent"] is False


def test_pre_turn_scope_asserted_buffered_until_first_turn():
    """SCOPE_ASSERTED firing before any turn-establishing CONTEXT_SNAPSHOT
    → buffered, then attributed to the first turn that arrives."""
    events = [
        # SA fires first (no turn established yet)
        _make_scope_asserted(
            "crm.early_call",
            advisory_flags=["INTENT_MISSING"],
            seq=2,
        ),
        # Then the first CONTEXT_SNAPSHOT establishes turn-1
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=300, completion_tokens=100, seq=3
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["undeclared_turn_count"] == 1
    assert result["undeclared_tokens"] == 400
    assert "INTENT_MISSING" in result["undeclared_turns"][0]["reasons"]
    assert "crm.early_call" in result["undeclared_turns"][0]["tool_ids"]


def test_unpaired_scope_asserted_at_session_end():
    """SCOPE_ASSERTED with no following turn-establishing CONTEXT_SNAPSHOT
    → unpaired_event_count increments; reasons don't attribute to any
    turn."""
    events = [
        _make_intent_declared(),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, seq=3
        ),
        # SA after the only turn — no following turn-establishing CS.
        _make_scope_asserted(
            "crm.late_call",
            advisory_flags=["INTENT_MISSING"],
            seq=4,
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["unpaired_event_count"] == 1
    # Turn 1 itself is not undeclared (no SAs attributed before its
    # establishing snapshot).
    assert result["undeclared_turn_count"] == 0
    assert result["undeclared_tokens"] == 0
    # Unpaired event flips status to partial (Checkpoint 2 status logic).
    assert result["status"] == STATUS_PARTIAL
    # And the warnings list has a corresponding entry.
    assert any(w["code"] == "unpaired_scope_asserted" for w in result["warnings"])


# ---------------------------------------------------------------------------
# Checkpoint 2 — edge cases, warning counters, dedupe precedence,
# malformed-trace handling, performance smoke, golden-trace.
# ---------------------------------------------------------------------------


def test_dedupe_no_conflict_when_identical_tokens():
    """Multiple CONTEXT_SNAPSHOTs sharing one llm_turn_id with IDENTICAL
    populated tokens → no dedupe_conflict. The v0.2.3 attribution
    contract: same usage attached to multiple events from one turn."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.read", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=300, completion_tokens=100, seq=4
        ),
        _make_scope_asserted("crm.list", seq=5),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=300, completion_tokens=100, seq=6
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["dedupe_conflict_count"] == 0
    assert result["status"] == STATUS_OK
    # Tokens counted once for the turn (not 2x, not 1x-per-event).
    assert result["total_tokens"] == 400


def test_dedupe_conflict_when_different_tokens():
    """Multiple CONTEXT_SNAPSHOTs sharing one llm_turn_id with
    DIFFERENT populated tokens → dedupe_conflict_count fires; warnings
    list gets an entry; status flips to partial. First populated wins."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.read", seq=3),
        # First CS — sets tokens for turn-1.
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=300, completion_tokens=100, seq=4
        ),
        _make_scope_asserted("crm.list", seq=5),
        # Second CS — same turn, DIFFERENT tokens. Conflict.
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=999, completion_tokens=999, seq=6
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["dedupe_conflict_count"] == 1
    assert result["status"] == STATUS_PARTIAL
    # First populated wins → original 300+100 stands.
    assert result["total_tokens"] == 400
    assert any(w["code"] == "dedupe_conflict" for w in result["warnings"])


def test_untokened_pair_count_when_tokens_without_turn_id():
    """CONTEXT_SNAPSHOT with populated tokens but no llm_turn_id →
    integrator misconfiguration. untokened_pair_count++; warning entry;
    status partial."""
    # First a normal turn so we don't hit no_token_data status.
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.read", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, seq=4
        ),
        # Now a CONTEXT_SNAPSHOT with populated tokens but no llm_turn_id.
        # Manually crafted (the helper requires turn_id when tokens given).
        {
            "event_type": "CONTEXT_SNAPSHOT",
            "session_id": "test-session",
            "event_sequence_number": 5,
            "agent_id": "test-agent",
            "payload": {
                "data_classifications": [],
                "classification_source": "unclassified",
                "provenance": [],
                "retention_flags": [],
                "context_size_tokens": 100,
                "llm_prompt_tokens": 999,  # populated but no llm_turn_id
            },
            "advisory_flags": [],
            "policy_violations": [],
        },
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["untokened_pair_count"] == 1
    assert result["status"] == STATUS_PARTIAL
    assert any(w["code"] == "untokened_pair" for w in result["warnings"])
    # The misconfigured event's tokens are NOT included in totals (no
    # turn to attribute to).
    assert result["total_tokens"] == 100


def test_malformed_event_non_dict():
    """Event that is not a dict (e.g. a string in the events list) →
    skipped; malformed_event_count++; warning entry."""
    events = [
        _make_intent_declared(),
        "this is not an event dict",  # malformed
        _make_scope_asserted("crm.read", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, seq=4
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["malformed_event_count"] == 1
    assert any(w["code"] == "malformed_event" for w in result["warnings"])
    # The well-formed events still process correctly.
    assert result["total_tokens"] == 100


def test_malformed_event_missing_event_type():
    """Event dict without event_type → malformed; skipped; counter
    increments; warning entry."""
    events = [
        _make_intent_declared(),
        {"session_id": "test-session", "payload": {}},  # no event_type
        _make_scope_asserted("crm.read", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, seq=4
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["malformed_event_count"] == 1
    assert any(w["code"] == "malformed_event" for w in result["warnings"])


def test_status_partial_when_over_25_percent_malformed():
    """If >25% of events are malformed → status flips to partial via the
    threshold check, regardless of whether other counters fire."""
    # 4 well-formed + 2 malformed = 33% malformed → over threshold.
    events = [
        _make_intent_declared(),
        "malformed1",
        _make_scope_asserted("crm.read", seq=3),
        "malformed2",
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=100, seq=4
        ),
        _make_intent_declared(),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["malformed_event_count"] == 2
    # 2/6 = 33% > 25%
    assert result["status"] == STATUS_PARTIAL


def test_intent_declared_arrives_mid_session():
    """INTENT_DECLARED appearing AFTER some SCOPE_ASSERTEDs: those
    earlier SAs with INTENT_MISSING still mark their attributed turn
    as undeclared (the flag was true at evaluation time; we don't
    retroactively rewrite). session_has_declared_intent flips to True
    once the INTENT_DECLARED is seen."""
    events = [
        # SA fires first with INTENT_MISSING (no intent declared yet)
        _make_scope_asserted(
            "crm.early",
            advisory_flags=["INTENT_MISSING"],
            seq=2,
        ),
        # CS establishes turn-1 — picks up the INTENT_MISSING reason.
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=200, seq=3
        ),
        # NOW intent gets declared (mid-session)
        _make_intent_declared(stated_objective="late-bound objective"),
        # Subsequent SA + CS with no flags
        _make_scope_asserted("crm.late", seq=5),
        _make_context_snapshot(
            llm_turn_id="turn-2", prompt_tokens=100, seq=6
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    # session_has_declared_intent flipped to True
    assert result["session_has_declared_intent"] is True
    # Turn 1 stays undeclared (the flag was honest at evaluation time)
    assert result["undeclared_turn_count"] == 1
    assert result["undeclared_tokens"] == 200
    assert result["declared_tokens"] == 100


def test_scope_asserted_with_both_intent_missing_and_pol_001():
    """A single SCOPE_ASSERTED carrying BOTH INTENT_MISSING (advisory)
    AND POL-001 (policy violation) → both reasons recorded for the turn."""
    events = [
        _make_scope_asserted(
            "crm.write",
            advisory_flags=["INTENT_MISSING"],
            policy_violations=["POL-001"],
            seq=2,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=300, seq=3
        ),
    ]
    result = compute_undeclared_intent_spend(events)

    assert result["undeclared_turn_count"] == 1
    reasons = result["undeclared_turns"][0]["reasons"]
    assert "INTENT_MISSING" in reasons
    assert "POL-001" in reasons


def test_negative_and_non_int_token_values_ignored():
    """Negative ints, floats, strings, and bools in token fields →
    treated as unpopulated. No crash. The valid fields still count."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.read", seq=3),
        # CS with mostly-bad token values; only completion_tokens is valid.
        {
            "event_type": "CONTEXT_SNAPSHOT",
            "session_id": "test-session",
            "event_sequence_number": 4,
            "agent_id": "test-agent",
            "payload": {
                "data_classifications": [],
                "classification_source": "unclassified",
                "provenance": [],
                "retention_flags": [],
                "context_size_tokens": 100,
                "llm_turn_id": "turn-1",
                "llm_prompt_tokens": -5,           # negative → ignored
                "llm_completion_tokens": 50,        # valid → kept
                "llm_cached_read_tokens": "100",    # str → ignored
                "llm_cached_write_tokens": 1.5,     # non-integral float → ignored
            },
            "advisory_flags": [],
            "policy_violations": [],
        },
    ]
    result = compute_undeclared_intent_spend(events)

    # Only completion_tokens (50) survived defensive normalization.
    assert result["total_tokens"] == 50


def test_performance_10k_events_under_1_second():
    """Performance smoke test per plan v3 acceptance criteria: 10,000
    events processed in under 1 second on commodity hardware. Single-
    pass O(n) algorithm."""
    import time

    # Build a 10k-event session: 1 INTENT_DECLARED + 2,500 turns × (1 SA + 1 CS each).
    events: list = [_make_intent_declared()]
    for i in range(2500):
        events.append(
            _make_scope_asserted(
                f"crm.tool_{i % 100}",
                advisory_flags=["INTENT_MISSING"] if i % 5 == 0 else [],
                seq=i * 4 + 2,
            )
        )
        events.append(
            _make_context_snapshot(
                llm_turn_id=f"turn-{i}",
                prompt_tokens=100 + (i % 50),
                completion_tokens=50,
                seq=i * 4 + 3,
            )
        )

    assert len(events) == 5001  # Just over 5k; let's bump to 10k.
    # Bump to 10k by duplicating turn shapes.
    for i in range(2500, 5000):
        events.append(
            _make_scope_asserted(f"crm.tool_{i % 100}", seq=i * 4 + 2)
        )
        events.append(
            _make_context_snapshot(
                llm_turn_id=f"turn-{i}",
                prompt_tokens=100,
                seq=i * 4 + 3,
            )
        )
    assert len(events) >= 10000

    start = time.perf_counter()
    result = compute_undeclared_intent_spend(events)
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"Analyzer too slow: {elapsed:.3f}s for {len(events)} events"
    # And it actually produced sensible output, not empty.
    assert result["total_turn_count"] >= 5000


def test_golden_trace_byte_stable():
    """Fixed input event list produces a known output dict, byte-stable
    across multiple invocations. This is the load-bearing
    pure-function-guarantee test: the analyzer's output schema is
    deterministic and any two callers see exactly the same bytes for
    the same input."""
    events = [
        _make_intent_declared(stated_objective="Generate Q1 report"),
        # Turn 1 — declared
        _make_scope_asserted("crm.get_customer", seq=3),
        _make_context_snapshot(
            llm_turn_id="turn-aaa", prompt_tokens=100, completion_tokens=50, seq=4
        ),
        # Turn 2 — undeclared via POL-001
        _make_scope_asserted(
            "postgres.query",
            policy_violations=["POL-001"],
            seq=5,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-bbb", prompt_tokens=400, completion_tokens=200, seq=6
        ),
    ]

    result_a = compute_undeclared_intent_spend(events)
    result_b = compute_undeclared_intent_spend(events)
    result_c = compute_undeclared_intent_spend(events)

    # Three separate calls with the same input must produce repr-equal
    # output. This catches: insertion-order drift in dicts, set-based
    # operations that aren't sorted, time-based fields, random IDs, etc.
    assert repr(result_a) == repr(result_b) == repr(result_c)

    # Pin the entire output structure as a regression baseline. If any
    # field name, ordering, or default value changes inadvertently,
    # this test fails loudly.
    expected = {
        "session_id": "test-session",
        "status": "ok",
        "session_has_declared_intent": True,
        "total_tokens": 750,
        "token_breakdown": {
            "prompt": 500,
            "completion": 250,
            "cached_read": 0,
            "cached_write": 0,
        },
        # F21 (v0.2.9): session-wide tool-call counts. Two SCOPE_ASSERTED
        # events in the golden trace (both READ); by_operation sums to total.
        "tool_calls": {
            "total": 2,
            "by_operation": {"execute": 0, "read": 2, "write": 0, "delete": 0},
            "by_tool": {"crm.get_customer": 1, "postgres.query": 1},
        },
        # IR-3 (v0.2.9): measured tool-token attribution. This golden trace
        # uses positional pairing (no tool_use_id -> llm_turn_id join), so
        # there is no per-turn tool association — A1/A2 report 0, never an
        # inferred fallback (P1-safe).
        "tool_token_attribution": {
            "tokens_on_turns_with_tool_calls": 0,
            "total_tokens": 750,
            "percent_of_total": 0.0,
            "by_tool": [],
            "by_tool_is_non_additive": True,
        },
        "undeclared_tokens": 600,
        "declared_tokens": 150,
        "undeclared_ratio": 0.8,
        "undeclared_percent": 80.0,
        "undeclared_turn_count": 1,
        "total_turn_count": 2,
        "undeclared_turns": [
            {
                "turn_id": "turn-bbb",
                "tokens": 600,
                "reasons": ["POL-001"],
                "tool_ids": ["postgres.query"],
            }
        ],
        "warnings": [],
        "unpaired_event_count": 0,
        "untokened_pair_count": 0,
        "dedupe_conflict_count": 0,
        "malformed_event_count": 0,
        # v0.2.5 — additive profile-aware fields. None / empty for a
        # v0.2.4-shaped trace (no profile metadata in events). Per plan
        # CP4 §"JSON output: existing fields unchanged; new fields
        # additive."
        "profile_fingerprint": None,
        "profile_loaded": None,
        "profile_schema_version": None,
        "high_consequence_events": [],
        "task_boundary_events": [],
    }
    assert result_a == expected


# ---------------------------------------------------------------------------
# F21 (v0.2.9) — tool calls as a first-class block.
# ---------------------------------------------------------------------------


def test_f21_tool_calls_block_counts_by_operation_and_tool():
    """Every SCOPE_ASSERTED is one tool call. The block counts by the four
    operation classes and by tool_id; by_operation sums to total for
    well-formed traces."""
    events = [
        _make_scope_asserted("Bash", operation_type="EXECUTE", seq=1),
        _make_scope_asserted("Bash", operation_type="EXECUTE", seq=2),
        _make_scope_asserted("Read", operation_type="READ", seq=3),
        _make_scope_asserted("Edit", operation_type="WRITE", seq=4),
        _make_scope_asserted("Bash", operation_type="DELETE", seq=5),
    ]
    tc = compute_undeclared_intent_spend(events)["tool_calls"]
    assert tc["total"] == 5
    assert tc["by_operation"] == {
        "execute": 2, "read": 1, "write": 1, "delete": 1,
    }
    # by_operation sums to total (the IR-4-style reconciliation invariant).
    assert sum(tc["by_operation"].values()) == tc["total"]
    assert tc["by_tool"] == {"Bash": 3, "Read": 1, "Edit": 1}


def test_f21_tool_calls_present_with_no_token_data():
    """Tool-call counts are independent of token data — a no_token_data
    session (SCOPE_ASSERTED but no token-bearing CONTEXT_SNAPSHOT) still
    surfaces what the agent did."""
    events = [_make_scope_asserted("Bash", operation_type="EXECUTE", seq=1)]
    result = compute_undeclared_intent_spend(events)
    assert result["status"] == "no_token_data"
    assert result["tool_calls"]["total"] == 1
    assert result["tool_calls"]["by_operation"]["execute"] == 1


def test_f21_tool_calls_empty_when_no_scope_asserted():
    """No SCOPE_ASSERTED → zeroed block with the stable four-class shape."""
    tc = compute_undeclared_intent_spend([])["tool_calls"]
    assert tc == {
        "total": 0,
        "by_operation": {"execute": 0, "read": 0, "write": 0, "delete": 0},
        "by_tool": {},
    }


# ---------------------------------------------------------------------------
# IR-3 (v0.2.9) — measured tool-token attribution (A1 + A2).
# ---------------------------------------------------------------------------


def test_ir3_multi_tool_turn_credits_both_a2_and_counts_a1_once():
    """A turn that fires two tools credits BOTH the full turn total (A2,
    full-turn-credit, non-additive), while A1 counts that turn once.

    turn-1 (1000 tok) fires Bash + Edit; turn-2 (500 tok) fires no tool.
    """
    events = [
        _make_intent_declared(),
        _make_scope_asserted(
            "Bash", operation_type="EXECUTE", tool_use_id="tu_bash", seq=2,
        ),
        _make_scope_asserted(
            "Edit", operation_type="WRITE", tool_use_id="tu_edit", seq=3,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-1", tool_use_ids=["tu_bash", "tu_edit"],
            prompt_tokens=1000, seq=4,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-2", prompt_tokens=500, seq=5,
        ),
    ]
    attr = compute_undeclared_intent_spend(events)["tool_token_attribution"]

    # A1 — turn-1 fired >=1 tool (counted once); turn-2 did not.
    assert attr["total_tokens"] == 1500
    assert attr["tokens_on_turns_with_tool_calls"] == 1000
    assert attr["percent_of_total"] == 66.7

    # A2 — both tools credited the FULL turn-1 total; non-additive.
    assert attr["by_tool_is_non_additive"] is True
    by_tool = {e["tool_id"]: e for e in attr["by_tool"]}
    assert by_tool["Bash"]["tokens"] == 1000
    assert by_tool["Bash"]["turn_count"] == 1
    assert by_tool["Edit"]["tokens"] == 1000
    assert by_tool["Edit"]["turn_count"] == 1
    # The non-additivity: A2 totals sum to 2000 > A1's 1000.
    assert sum(e["tokens"] for e in attr["by_tool"]) == 2000


def test_ir3_zero_without_tool_use_id_join_never_inferred():
    """Without the tool_use_id -> llm_turn_id join (older positional
    traces), there is no per-turn tool association — A1/A2 report 0,
    never an inferred per-tool split (P1-safe)."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted("crm.read", seq=2),  # no tool_use_id
        _make_context_snapshot(
            llm_turn_id="turn-1", prompt_tokens=900, seq=3,  # no tool_use_ids
        ),
    ]
    attr = compute_undeclared_intent_spend(events)["tool_token_attribution"]
    assert attr["total_tokens"] == 900
    assert attr["tokens_on_turns_with_tool_calls"] == 0
    assert attr["percent_of_total"] == 0.0
    assert attr["by_tool"] == []


def test_ir3_single_tool_turn_a1_equals_a2():
    """One tool on one turn: A1 == that turn's tokens, and A2 credits the
    single tool the same amount (additive in the trivial single-tool case)."""
    events = [
        _make_intent_declared(),
        _make_scope_asserted(
            "Bash", operation_type="EXECUTE", tool_use_id="tu1", seq=2,
        ),
        _make_context_snapshot(
            llm_turn_id="turn-1", tool_use_ids=["tu1"],
            prompt_tokens=400, completion_tokens=100, seq=3,
        ),
    ]
    attr = compute_undeclared_intent_spend(events)["tool_token_attribution"]
    assert attr["tokens_on_turns_with_tool_calls"] == 500
    assert attr["percent_of_total"] == 100.0
    assert attr["by_tool"] == [
        {"tool_id": "Bash", "tokens": 500, "turn_count": 1},
    ]
