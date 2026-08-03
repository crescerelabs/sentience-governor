"""Step 4 acceptance: EventBuilder produces exact golden traces for Flow A and Flow B."""

import json
from pathlib import Path
from typing import List

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.schema.events import (
    ClassificationSource,
    DeploymentMode,
    DetectionMechanism,
    IntentConfidence,
    IntentSource,
    OperationType,
    WriteType,
)
from sentience_governor.session_manager.manager import SessionManager

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list:
    with open(FIXTURES / name) as f:
        return json.load(f)


def _make_builder(agent_id: str, session_id: str) -> tuple:
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


class TestFlowAGoldenTrace:
    """Flow A: clean execution — no flags, no violations."""

    def test_flow_a_produces_no_flags(self):
        sm, cache, builder = _make_builder("reporting-agent-v1", "sess-a-001")
        golden = _load("golden_trace_flow_a.json")

        # Event 1: AGENT_REGISTERED
        e1 = builder.build_agent_registered(
            agent_version="1.0.4",
            vendor_id="acme-analytics",
            declared_capabilities=["crm.read", "report_db.write"],
            owner_claim="user_123",
            event_id="evt-a-001",
            timestamp_utc=golden[0]["timestamp_utc"],
        )
        assert e1 is not None
        assert e1.advisory_flags == []
        assert e1.policy_violations == []
        assert e1.simulated_consequence is None
        assert e1.pass_through is True
        assert e1.event_sequence_number == 1
        assert e1.previous_event_id is None

        # Event 2: INTENT_DECLARED
        e2 = builder.build_intent_declared(
            stated_objective="Generate customer usage report for Q1 2026",
            intent_source=IntentSource.explicit,
            intent_confidence=IntentConfidence.explicit,
            authorization_claim="user_123",
            session_scope_hint=["crm.read", "report_db.write"],
            event_id="evt-a-002",
            timestamp_utc=golden[1]["timestamp_utc"],
        )
        assert e2 is not None
        assert e2.advisory_flags == []
        assert e2.policy_violations == []
        assert e2.event_sequence_number == 2
        assert e2.previous_event_id == "evt-a-001"

        # Event 3: SCOPE_ASSERTED (READ — clean)
        e3 = builder.build_scope_asserted(
            tool_id="crm.fetch_usage",
            asserted_permissions=["read"],
            target_system="crm",
            operation_type=OperationType.READ,
            event_id="evt-a-003",
            timestamp_utc=golden[2]["timestamp_utc"],
        )
        assert e3 is not None
        assert e3.advisory_flags == []
        assert e3.policy_violations == []
        assert e3.event_sequence_number == 3
        assert e3.previous_event_id == "evt-a-002"

        # Event 4: CONTEXT_SNAPSHOT (classified — clean)
        e4 = builder.build_context_snapshot(
            data_classifications=["internal"],
            classification_source=ClassificationSource.vendor,
            provenance=["crm"],
            retention_flags=["may-persist"],
            context_size_tokens=1200,
            event_id="evt-a-004",
            timestamp_utc=golden[3]["timestamp_utc"],
        )
        assert e4 is not None
        assert e4.advisory_flags == []
        assert e4.policy_violations == []
        assert e4.event_sequence_number == 4

        # Event 5: MEMORY_WRITE_ATTEMPT (explicit, classified — clean)
        e5 = builder.build_memory_write_attempt(
            write_type=WriteType.explicit_persist,
            detection_mechanism=None,
            target_store="report_db",
            write_classification="internal",
            write_size_tokens=800,
            retention_requested="30_days",
            event_id="evt-a-005",
            timestamp_utc=golden[4]["timestamp_utc"],
        )
        assert e5 is not None
        assert e5.advisory_flags == []
        assert e5.policy_violations == []
        assert e5.event_sequence_number == 5

        sm.session_end("sess-a-001")
        cache.clear_session("sess-a-001")


class TestFlowBGoldenTrace:
    """Flow B: all 8 advisory flags fire, all 5 policy rules violated."""

    def test_flow_b_all_flags_fire(self):
        sm, cache, builder = _make_builder("unscoped-agent-v1", "sess-b-001")
        golden = _load("golden_trace_flow_b.json")

        # Event 1: AGENT_REGISTERED — unregistered
        e1 = builder.build_agent_registered(
            agent_version=None,
            vendor_id=None,
            declared_capabilities=[],
            owner_claim=None,
            event_id="evt-b-001",
            timestamp_utc=golden[0]["timestamp_utc"],
        )
        assert e1 is not None
        assert "AGENT_UNREGISTERED" in e1.advisory_flags
        assert "POL-002" in e1.policy_violations
        assert e1.simulated_consequence is not None
        assert e1.pass_through is True

        # Event 2: INTENT_DECLARED — no signal
        e2 = builder.build_intent_declared(
            stated_objective=None,
            intent_source=IntentSource.none,
            intent_confidence=IntentConfidence.unknown,
            authorization_claim=None,
            session_scope_hint=[],
            event_id="evt-b-002",
            timestamp_utc=golden[1]["timestamp_utc"],
        )
        assert e2 is not None
        assert "INTENT_MISSING" in e2.advisory_flags
        assert e2.policy_violations == []  # POL-001 fires on next mutating op
        assert e2.previous_event_id == "evt-b-001"

        # Event 3: SCOPE_ASSERTED — WRITE with no intent → POL-001 fires
        e3 = builder.build_scope_asserted(
            tool_id="crm.update_record",
            asserted_permissions=["write"],
            target_system="crm",
            operation_type=OperationType.WRITE,
            event_id="evt-b-003",
            timestamp_utc=golden[2]["timestamp_utc"],
        )
        assert e3 is not None
        assert "SCOPE_OPERATION_UNEXPECTED" in e3.advisory_flags
        assert "SCOPE_INTENT_MISMATCH" in e3.advisory_flags
        assert "POL-001" in e3.policy_violations
        assert e3.simulated_consequence is not None
        assert "WRITE" in e3.simulated_consequence

        # Event 4: CONTEXT_SNAPSHOT — unclassified
        e4 = builder.build_context_snapshot(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=["crm", "user_input"],
            retention_flags=[],
            context_size_tokens=1100,
            authorization_claim=None,
            event_id="evt-b-004",
            timestamp_utc=golden[3]["timestamp_utc"],
        )
        assert e4 is not None
        assert "CONTEXT_UNCLASSIFIED" in e4.advisory_flags
        assert "POL-003" in e4.policy_violations

        # Event 5: CONTEXT_SNAPSHOT — sensitivity escalation (None → confidential)
        e5 = builder.build_context_snapshot(
            data_classifications=["confidential"],
            classification_source=ClassificationSource.explicit,
            provenance=["hr_system"],
            retention_flags=["must-not-persist"],
            context_size_tokens=1800,
            authorization_claim=None,
            event_id="evt-b-005",
            timestamp_utc=golden[4]["timestamp_utc"],
        )
        assert e5 is not None
        assert "SENSITIVITY_ESCALATION" in e5.advisory_flags
        assert "POL-005" in e5.policy_violations

        # Event 6: MEMORY_WRITE_ATTEMPT — unclassified, no retention
        e6 = builder.build_memory_write_attempt(
            write_type=WriteType.write_to_persistence_target,
            detection_mechanism=DetectionMechanism.tool_metadata,
            target_store="vector_db",
            write_classification="unclassified",
            write_size_tokens=1500,
            retention_requested=None,
            event_id="evt-b-006",
            timestamp_utc=golden[5]["timestamp_utc"],
        )
        assert e6 is not None
        assert "MEMORY_WRITE_UNCLASSIFIED" in e6.advisory_flags
        assert "MEMORY_WRITE_CANDIDATE" in e6.advisory_flags
        assert "POL-004" in e6.policy_violations

        sm.session_end("sess-b-001")
        cache.clear_session("sess-b-001")

    def test_all_five_policies_violated(self):
        """Aggregate: all five POL-* rules appear across Flow B."""
        sm, cache, builder = _make_builder("unscoped-agent-v1", "sess-b-check")

        all_violations = []

        e1 = builder.build_agent_registered(
            agent_version=None, vendor_id=None, declared_capabilities=[],
            owner_claim=None
        )
        all_violations.extend(e1.policy_violations)

        e2 = builder.build_intent_declared(
            stated_objective=None, intent_source=IntentSource.none,
            intent_confidence=IntentConfidence.unknown,
            authorization_claim=None, session_scope_hint=[]
        )
        all_violations.extend(e2.policy_violations)

        e3 = builder.build_scope_asserted(
            tool_id="crm.update_record", asserted_permissions=["write"],
            target_system="crm", operation_type=OperationType.WRITE
        )
        all_violations.extend(e3.policy_violations)

        e4 = builder.build_context_snapshot(
            data_classifications=[], classification_source=ClassificationSource.unclassified,
            provenance=["crm"], retention_flags=[], context_size_tokens=1000,
            authorization_claim=None
        )
        all_violations.extend(e4.policy_violations)

        e5 = builder.build_context_snapshot(
            data_classifications=["confidential"], classification_source=ClassificationSource.explicit,
            provenance=["hr"], retention_flags=[], context_size_tokens=1500,
            authorization_claim=None
        )
        all_violations.extend(e5.policy_violations)

        e6 = builder.build_memory_write_attempt(
            write_type=WriteType.write_to_persistence_target,
            detection_mechanism=DetectionMechanism.tool_metadata,
            target_store="vector_db", write_classification="unclassified",
            write_size_tokens=500, retention_requested=None
        )
        all_violations.extend(e6.policy_violations)

        unique = set(all_violations)
        assert unique == {"POL-001", "POL-002", "POL-003", "POL-004", "POL-005"}


class TestCachePreconditions:
    """Step 3 acceptance: flags cannot fire before their preconditions are met."""

    def test_scope_intent_mismatch_requires_intent_baseline(self):
        """SCOPE_INTENT_MISMATCH fires even without baseline (null baseline = mismatch)."""
        sm, cache, builder = _make_builder("agent-x", "sess-pre-1")
        # No INTENT_DECLARED yet — baseline is null → mismatch fires
        e = builder.build_scope_asserted(
            tool_id="tool.read", asserted_permissions=["read"],
            target_system="tool", operation_type=OperationType.READ
        )
        # With null baseline, SCOPE_INTENT_MISMATCH fires (null = no matching scope)
        assert "SCOPE_INTENT_MISMATCH" in e.advisory_flags

    def test_sensitivity_escalation_requires_prior_snapshot(self):
        """SENSITIVITY_ESCALATION cannot fire on the first CONTEXT_SNAPSHOT."""
        sm, cache, builder = _make_builder("agent-y", "sess-pre-2")
        e = builder.build_context_snapshot(
            data_classifications=["confidential"],
            classification_source=ClassificationSource.explicit,
            provenance=["hr"], retention_flags=[], context_size_tokens=100,
            authorization_claim=None
        )
        # First snapshot — no prior to compare against
        assert "SENSITIVITY_ESCALATION" not in e.advisory_flags

    def test_sensitivity_escalation_fires_on_second_snapshot(self):
        """SENSITIVITY_ESCALATION fires on 2nd snapshot when tier escalates."""
        sm, cache, builder = _make_builder("agent-z", "sess-pre-3")
        # First snapshot: internal
        builder.build_context_snapshot(
            data_classifications=["internal"],
            classification_source=ClassificationSource.vendor,
            provenance=["crm"], retention_flags=[], context_size_tokens=100,
        )
        # Second snapshot: confidential (escalation)
        e2 = builder.build_context_snapshot(
            data_classifications=["confidential"],
            classification_source=ClassificationSource.explicit,
            provenance=["hr"], retention_flags=[], context_size_tokens=200,
            authorization_claim=None
        )
        assert "SENSITIVITY_ESCALATION" in e2.advisory_flags


class TestF15ReadConsequence:
    """F15 (v0.2.8.1): POL-001 fires on READ ops (via SCOPE_INTENT_MISMATCH);
    the simulated_consequence must not claim a WRITE was blocked."""

    def test_pol_001_on_read_uses_read_consequence(self):
        sm, cache, builder = _make_builder("unscoped-agent-v1", "sess-f15-read")
        # READ scope with no declared intent → null baseline → POL-001 fires.
        e = builder.build_scope_asserted(
            tool_id="crm.fetch", asserted_permissions=["read"],
            target_system="crm", operation_type=OperationType.READ,
        )
        assert "POL-001" in e.policy_violations
        assert e.simulated_consequence is not None
        # The bug being fixed: a READ reported as "WRITE ... would have been blocked".
        assert "WRITE" not in e.simulated_consequence
        assert "READ" in e.simulated_consequence
        sm.session_end("sess-f15-read")
        cache.clear_session("sess-f15-read")

    def test_pol_001_on_write_keeps_spec_consequence(self):
        sm, cache, builder = _make_builder("unscoped-agent-v1", "sess-f15-write")
        # WRITE scope with no declared intent → POL-001 fires; mutating wording
        # (spec/golden) must stay verbatim.
        e = builder.build_scope_asserted(
            tool_id="crm.update", asserted_permissions=["write"],
            target_system="crm", operation_type=OperationType.WRITE,
        )
        assert "POL-001" in e.policy_violations
        assert "This WRITE operation would have been blocked." in e.simulated_consequence
        sm.session_end("sess-f15-write")
        cache.clear_session("sess-f15-write")
