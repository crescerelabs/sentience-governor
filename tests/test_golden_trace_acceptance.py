"""Golden Trace Acceptance Test.

This is the canonical end-to-end proof that the Sentience Governor runtime
produces the reference traces defined in the Golden Trace Package. It
drives the full composition pipeline (SessionManager + InProcessCache +
EventBuilder) with the inputs that are supposed to produce each flow,
serialises the resulting events, and asserts the serialised list matches
the checked-in fixture byte-for-byte at the field level.

Why not through wrap_mcp_client?
--------------------------------
The current v0 wrapper (``sentience_governor.wrapper.mcp``) produces
context snapshots and memory writes with defaulted, unclassified fields
because it has no hook for an agent or tool to inject classification
metadata. Flow A's CONTEXT_SNAPSHOT carries
``data_classifications=["internal"]`` with ``classification_source=vendor``
— values that cannot come from the wrapper's current code path.

The acceptance test therefore drives the EventBuilder directly, which IS
the layer the Tech Spec refers to as the canonical reference. The
wrapper is a convenience composition on top; adding a classification
injection hook to it is a separate design decision, not something this
test should paper over.

Field normalisation
-------------------
Every event_id, timestamp_utc, session_id, and agent_id in the fixture
is reproducible because ``EventBuilder.build_*`` accepts ``event_id``
and ``timestamp_utc`` overrides, and SessionManager + agent_id are
supplied at construction time. No id or timestamp masking is applied —
the values come through end-to-end and the comparison is byte-strict.

One targeted relaxation: ``advisory_flags`` and ``policy_violations``
are compared as unordered sets rather than ordered lists. The schema
types them as ``List[str]`` but there is no documented ordering
contract, and the order a builder adds them depends on the internal
order of its checks. Treating them as sets is the honest match for
their semantics: they are the set of concerns that fired, not a
sequence. Every OTHER field in every event is compared byte-strict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.schema.events import (
    ClassificationSource,
    DeploymentMode,
    DetectionMechanism,
    GovernanceEvent,
    IntentConfidence,
    IntentSource,
    OperationType,
    WriteType,
)
from sentience_governor.session_manager.manager import SessionManager


FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_fixture(name: str) -> List[Dict[str, Any]]:
    """Load a golden trace fixture as a list of plain dicts."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _serialise(event: GovernanceEvent) -> Dict[str, Any]:
    """Serialise a GovernanceEvent to a plain dict with JSON-compatible values.

    Round-trips through ``model_dump_json`` + ``json.loads`` so enums
    become their string values, sub-models become dicts, and the result
    is byte-compatible with what would be written to the fixture file.
    """
    return json.loads(event.model_dump_json())


# Fields whose list order is NOT a contract. The schema types these as
# List[str] but they are semantically unordered sets of concerns. Comparing
# them as sorted lists preserves value equality while tolerating the
# accidental ordering of the builder's internal checks vs. the hand-authored
# fixture.
_UNORDERED_LIST_FIELDS = ("advisory_flags", "policy_violations")


def _normalise_for_comparison(event: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``event`` with unordered-list fields sorted.

    Only ``advisory_flags`` and ``policy_violations`` are touched. Every
    other field is left exactly as it appears in the serialised event
    (or the fixture) so the comparison is byte-strict on everything that
    has a defined ordering.
    """
    normalised = dict(event)
    for key in _UNORDERED_LIST_FIELDS:
        value = normalised.get(key)
        if isinstance(value, list):
            normalised[key] = sorted(value)
    return normalised


def _assert_events_match(
    actual: List[GovernanceEvent], expected: List[Dict[str, Any]]
) -> None:
    """Assert each event in ``actual`` matches the corresponding fixture
    entry field-by-field. Every field except advisory_flags and
    policy_violations is compared strict; those two are compared as
    unordered sets. Fails loudly per-event on the first mismatch with a
    full diff."""
    assert len(actual) == len(expected), (
        f"event count mismatch: actual={len(actual)} expected={len(expected)}"
    )
    for idx, (event, fixture) in enumerate(zip(actual, expected)):
        serialised = _normalise_for_comparison(_serialise(event))
        fixture_norm = _normalise_for_comparison(fixture)
        if serialised != fixture_norm:
            diff_lines = []
            keys = sorted(set(serialised) | set(fixture_norm))
            for key in keys:
                a = serialised.get(key, "<missing>")
                e = fixture_norm.get(key, "<missing>")
                marker = "  " if a == e else "!="
                diff_lines.append(f"    {marker} {key}: actual={a!r} expected={e!r}")
            raise AssertionError(
                f"Event {idx} ({fixture.get('event_type', '?')}) does not match fixture:\n"
                + "\n".join(diff_lines)
            )


def _fresh_builder(agent_id: str, session_id: str) -> tuple[
    SessionManager, InProcessCache, EventBuilder
]:
    """Construct a fresh SessionManager / Cache / EventBuilder trio with an
    active session. Matches the setup in tests/test_event_builder.py."""
    sm = SessionManager()
    cache = InProcessCache()
    sm.session_start(session_id=session_id, agent_id=agent_id)
    cache.init_session(session_id)
    builder = EventBuilder(
        session_manager=sm,
        cache=cache,
        agent_id=agent_id,
        session_id=session_id,
        deployment_mode=DeploymentMode.vendor_managed,
    )
    return sm, cache, builder


# ---------------------------------------------------------------------------
# Flow A — canonical clean execution
# ---------------------------------------------------------------------------

class TestFlowAAcceptance:
    """Flow A is the reference trace for a clean, well-governed agent run:
    five events, no flags, no violations, a single READ tool call against
    a CRM, a classified context snapshot, and an explicit memory write
    with classification and retention metadata.

    The test drives the EventBuilder with the exact inputs that should
    reproduce the fixture and asserts the serialised event list is
    byte-for-byte identical.
    """

    def test_flow_a_full_trace_matches_fixture_byte_for_byte(self) -> None:
        fixture = _load_fixture("golden_trace_flow_a.json")

        sm, cache, builder = _fresh_builder(
            agent_id="reporting-agent-v1",
            session_id="sess-a-001",
        )

        events: List[GovernanceEvent] = []

        # --- Event 1: AGENT_REGISTERED ---
        events.append(
            builder.build_agent_registered(
                agent_version="1.0.4",
                vendor_id="acme-analytics",
                declared_capabilities=["crm.read", "report_db.write"],
                owner_claim="user_123",
                event_id="evt-a-001",
                timestamp_utc="2026-04-14T09:00:00.000Z",
            )
        )

        # --- Event 2: INTENT_DECLARED (explicit, clean) ---
        events.append(
            builder.build_intent_declared(
                stated_objective="Generate customer usage report for Q1 2026",
                intent_source=IntentSource.explicit,
                intent_confidence=IntentConfidence.explicit,
                authorization_claim="user_123",
                session_scope_hint=["crm.read", "report_db.write"],
                event_id="evt-a-002",
                timestamp_utc="2026-04-14T09:00:00.183Z",
            )
        )

        # --- Event 3: SCOPE_ASSERTED (READ — clean) ---
        events.append(
            builder.build_scope_asserted(
                tool_id="crm.fetch_usage",
                asserted_permissions=["read"],
                target_system="crm",
                operation_type=OperationType.READ,
                event_id="evt-a-003",
                timestamp_utc="2026-04-14T09:00:00.412Z",
            )
        )

        # --- Event 4: CONTEXT_SNAPSHOT (classified, vendor-tagged) ---
        events.append(
            builder.build_context_snapshot(
                data_classifications=["internal"],
                classification_source=ClassificationSource.vendor,
                provenance=["crm"],
                retention_flags=["may-persist"],
                context_size_tokens=1200,
                authorization_claim="user_123",
                event_id="evt-a-004",
                timestamp_utc="2026-04-14T09:00:00.891Z",
            )
        )

        # --- Event 5: MEMORY_WRITE_ATTEMPT (explicit, classified) ---
        events.append(
            builder.build_memory_write_attempt(
                write_type=WriteType.explicit_persist,
                detection_mechanism=None,
                target_store="report_db",
                write_classification="internal",
                write_size_tokens=800,
                retention_requested="30_days",
                event_id="evt-a-005",
                timestamp_utc="2026-04-14T09:00:01.204Z",
            )
        )

        try:
            _assert_events_match(events, fixture)
        finally:
            sm.session_end("sess-a-001")
            cache.clear_session("sess-a-001")

    # -----------------------------------------------------------------
    # Per-event invariants — belt-and-suspenders against per-field drift
    # -----------------------------------------------------------------

    def test_flow_a_sequence_numbers_are_monotonic(self) -> None:
        """event_sequence_number must increase monotonically from 1 to 5."""
        fixture = _load_fixture("golden_trace_flow_a.json")
        seqs = [e["event_sequence_number"] for e in fixture]
        assert seqs == [1, 2, 3, 4, 5]

    def test_flow_a_previous_event_id_chain_is_consistent(self) -> None:
        """Every event's previous_event_id must equal the preceding
        event's event_id, and the first must be null."""
        fixture = _load_fixture("golden_trace_flow_a.json")
        assert fixture[0]["previous_event_id"] is None
        for i in range(1, len(fixture)):
            assert fixture[i]["previous_event_id"] == fixture[i - 1]["event_id"], (
                f"Event {i} previous_event_id chain broken"
            )

    def test_flow_a_has_zero_flags_and_violations(self) -> None:
        """Flow A is the clean reference — no advisory flags, no policy
        violations, no simulated consequences, pass_through=true for every
        event. Any drift breaks the 'clean execution' contract."""
        fixture = _load_fixture("golden_trace_flow_a.json")
        for idx, event in enumerate(fixture):
            assert event["advisory_flags"] == [], (
                f"Event {idx} has advisory_flags: {event['advisory_flags']}"
            )
            assert event["policy_violations"] == [], (
                f"Event {idx} has policy_violations: {event['policy_violations']}"
            )
            assert event["simulated_consequence"] is None, (
                f"Event {idx} has simulated_consequence: {event['simulated_consequence']}"
            )
            assert event["pass_through"] is True, (
                f"Event {idx} has pass_through: {event['pass_through']}"
            )


# ---------------------------------------------------------------------------
# Flow B — canonical failure trace (every flag, every rule)
# ---------------------------------------------------------------------------

class TestFlowBAcceptance:
    """Flow B is the reference failure trace.

    It is designed to exercise every advisory flag (all 8) and every
    policy rule (POL-001 through POL-005) in a single run. It also
    exercises the cache's ``_NO_PRIOR`` sentinel path: the first
    ``CONTEXT_SNAPSHOT`` establishes the prior sensitivity tier and
    cannot itself fire ``SENSITIVITY_ESCALATION``; the second snapshot
    escalates from unclassified to ``confidential`` and must fire it.

    Event sequence:

    1. AGENT_REGISTERED   — agent_version/vendor/owner all None
                            → AGENT_UNREGISTERED, POL-002
    2. INTENT_DECLARED    — no signal (source=none, confidence=unknown)
                            → INTENT_MISSING
    3. SCOPE_ASSERTED     — WRITE with no prior intent
                            → SCOPE_OPERATION_UNEXPECTED,
                              SCOPE_INTENT_MISMATCH, POL-001
    4. CONTEXT_SNAPSHOT   — unclassified
                            → CONTEXT_UNCLASSIFIED, POL-003
    5. CONTEXT_SNAPSHOT   — confidential (escalation from prior)
                            → SENSITIVITY_ESCALATION, POL-005
    6. MEMORY_WRITE_ATTEMPT — unclassified, no retention
                            → MEMORY_WRITE_UNCLASSIFIED,
                              MEMORY_WRITE_CANDIDATE, POL-004
    """

    def test_flow_b_full_trace_matches_fixture_byte_for_byte(self) -> None:
        fixture = _load_fixture("golden_trace_flow_b.json")

        sm, cache, builder = _fresh_builder(
            agent_id="unscoped-agent-v1",
            session_id="sess-b-001",
        )

        events: List[GovernanceEvent] = []

        # --- Event 1: AGENT_REGISTERED — unregistered agent ---
        events.append(
            builder.build_agent_registered(
                agent_version=None,
                vendor_id=None,
                declared_capabilities=[],
                owner_claim=None,
                event_id="evt-b-001",
                timestamp_utc="2026-04-14T10:00:00.000Z",
            )
        )

        # --- Event 2: INTENT_DECLARED — no signal ---
        events.append(
            builder.build_intent_declared(
                stated_objective=None,
                intent_source=IntentSource.none,
                intent_confidence=IntentConfidence.unknown,
                authorization_claim=None,
                session_scope_hint=[],
                event_id="evt-b-002",
                timestamp_utc="2026-04-14T10:00:00.121Z",
            )
        )

        # --- Event 3: SCOPE_ASSERTED — WRITE with no prior intent ---
        events.append(
            builder.build_scope_asserted(
                tool_id="crm.update_record",
                asserted_permissions=["write"],
                target_system="crm",
                operation_type=OperationType.WRITE,
                event_id="evt-b-003",
                timestamp_utc="2026-04-14T10:00:00.318Z",
            )
        )

        # --- Event 4: CONTEXT_SNAPSHOT — unclassified ---
        events.append(
            builder.build_context_snapshot(
                data_classifications=[],
                classification_source=ClassificationSource.unclassified,
                provenance=["crm", "user_input"],
                retention_flags=[],
                context_size_tokens=1100,
                authorization_claim=None,
                event_id="evt-b-004",
                timestamp_utc="2026-04-14T10:00:00.729Z",
            )
        )

        # --- Event 5: CONTEXT_SNAPSHOT — sensitivity escalation ---
        # The cache now has a prior tier (unclassified from event 4), so
        # this snapshot's confidential tier is a real escalation and
        # must fire SENSITIVITY_ESCALATION.
        events.append(
            builder.build_context_snapshot(
                data_classifications=["confidential"],
                classification_source=ClassificationSource.explicit,
                provenance=["hr_system"],
                retention_flags=["must-not-persist"],
                context_size_tokens=1800,
                authorization_claim=None,
                event_id="evt-b-005",
                timestamp_utc="2026-04-14T10:00:01.103Z",
            )
        )

        # --- Event 6: MEMORY_WRITE_ATTEMPT — unclassified, no retention ---
        events.append(
            builder.build_memory_write_attempt(
                write_type=WriteType.write_to_persistence_target,
                detection_mechanism=DetectionMechanism.tool_metadata,
                target_store="vector_db",
                write_classification="unclassified",
                write_size_tokens=1500,
                retention_requested=None,
                event_id="evt-b-006",
                timestamp_utc="2026-04-14T10:00:01.587Z",
            )
        )

        try:
            _assert_events_match(events, fixture)
        finally:
            sm.session_end("sess-b-001")
            cache.clear_session("sess-b-001")

    # -----------------------------------------------------------------
    # Coverage invariants — pinned against drift in the fixture itself
    # -----------------------------------------------------------------

    def test_flow_b_every_policy_rule_fires_exactly_once(self) -> None:
        """All five POL rules must appear exactly once across the
        Flow B fixture. Pins the 'every rule exercised' contract."""
        fixture = _load_fixture("golden_trace_flow_b.json")
        all_violations: List[str] = []
        for event in fixture:
            all_violations.extend(event.get("policy_violations", []))
        assert sorted(all_violations) == [
            "POL-001",
            "POL-002",
            "POL-003",
            "POL-004",
            "POL-005",
        ]

    def test_flow_b_every_advisory_flag_fires_exactly_once(self) -> None:
        """All eight advisory flags must appear exactly once across
        Flow B. The complete flag set is pinned here as a literal set
        comparison so any addition or removal in AdvisoryFlag forces a
        corresponding fixture update (and vice versa)."""
        fixture = _load_fixture("golden_trace_flow_b.json")
        all_flags: List[str] = []
        for event in fixture:
            all_flags.extend(event.get("advisory_flags", []))
        assert sorted(all_flags) == sorted(
            [
                "AGENT_UNREGISTERED",
                "INTENT_MISSING",
                "SCOPE_OPERATION_UNEXPECTED",
                "SCOPE_INTENT_MISMATCH",
                "CONTEXT_UNCLASSIFIED",
                "SENSITIVITY_ESCALATION",
                "MEMORY_WRITE_UNCLASSIFIED",
                "MEMORY_WRITE_CANDIDATE",
            ]
        )

    def test_flow_b_sensitivity_escalation_requires_prior_snapshot(
        self,
    ) -> None:
        """The _NO_PRIOR sentinel in the cache ensures SENSITIVITY_ESCALATION
        cannot fire on the first CONTEXT_SNAPSHOT of a session — there is
        no prior tier to compare against. It must fire on the second
        snapshot when the tier escalates.

        This is the single subtlest piece of cache logic in the runtime
        and the whole reason Flow B has two CONTEXT_SNAPSHOT events
        instead of one. Locking the contract here means a regression in
        the cache's sentinel handling will fail this specific test
        rather than being buried in a generic byte-match failure.
        """
        fixture = _load_fixture("golden_trace_flow_b.json")
        event_4 = fixture[3]
        event_5 = fixture[4]
        assert event_4["event_type"] == "CONTEXT_SNAPSHOT"
        assert event_5["event_type"] == "CONTEXT_SNAPSHOT"
        assert "SENSITIVITY_ESCALATION" not in event_4["advisory_flags"], (
            "First CONTEXT_SNAPSHOT must not fire SENSITIVITY_ESCALATION"
        )
        assert "SENSITIVITY_ESCALATION" in event_5["advisory_flags"], (
            "Second CONTEXT_SNAPSHOT must fire SENSITIVITY_ESCALATION"
        )

    def test_flow_b_every_event_has_pass_through_true(self) -> None:
        """Open tier contract: every event in Flow B, even when flagged,
        still has pass_through=true. The wrapper never blocks; it only
        observes. Any regression that sets pass_through=false would
        fail this test immediately."""
        fixture = _load_fixture("golden_trace_flow_b.json")
        for idx, event in enumerate(fixture):
            assert event["pass_through"] is True, (
                f"Event {idx} has pass_through={event['pass_through']}; "
                "the open tier must never block"
            )
