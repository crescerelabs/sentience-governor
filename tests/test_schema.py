"""Step 1 acceptance: all six event types instantiate correctly and match
golden trace schemas."""

import json
from pathlib import Path

import pytest

from sentience_governor.schema.events import (
    AgentRegisteredPayload,
    ClassificationSource,
    ContextSnapshotPayload,
    DeploymentMode,
    DetectionMechanism,
    ErrorType,
    EventType,
    GovernanceErrorPayload,
    GovernanceEvent,
    IntentConfidence,
    IntentDeclaredPayload,
    IntentSource,
    InterceptStage,
    MemoryWriteAttemptPayload,
    OperationType,
    PrimitiveType,
    ScopeAssertedPayload,
    Severity,
    WriteType,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list:
    with open(FIXTURES / name) as f:
        return json.load(f)


class TestAllSixEventTypes:
    def test_agent_registered(self):
        p = AgentRegisteredPayload(
            agent_id="reporting-agent-v1",
            agent_version="1.0.4",
            vendor_id="acme-analytics",
            deployment_mode=DeploymentMode.vendor_managed,
            declared_capabilities=["crm.read", "report_db.write"],
            owner_claim="user_123",
        )
        e = GovernanceEvent(
            event_id="evt-a-001",
            event_type=EventType.AGENT_REGISTERED,
            session_id="sess-a-001",
            event_sequence_number=1,
            previous_event_id=None,
            agent_id="reporting-agent-v1",
            deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="2026-04-14T09:00:00.000Z",
            primitive=PrimitiveType.REGISTRATION,
            payload=p,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        assert e.event_type == EventType.AGENT_REGISTERED
        assert e.pass_through is True

    def test_intent_declared(self):
        p = IntentDeclaredPayload(
            stated_objective="Generate customer usage report for Q1 2026",
            intent_source=IntentSource.explicit,
            intent_confidence=IntentConfidence.explicit,
            authorization_claim="user_123",
            session_scope_hint=["crm.read", "report_db.write"],
        )
        e = GovernanceEvent(
            event_id="evt-a-002",
            event_type=EventType.INTENT_DECLARED,
            session_id="sess-a-001",
            event_sequence_number=2,
            previous_event_id="evt-a-001",
            agent_id="reporting-agent-v1",
            deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="2026-04-14T09:00:00.183Z",
            primitive=PrimitiveType.INTENT,
            payload=p,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        assert e.event_type == EventType.INTENT_DECLARED

    def test_scope_asserted(self):
        p = ScopeAssertedPayload(
            tool_id="crm.fetch_usage",
            asserted_permissions=["read"],
            target_system="crm",
            operation_type=OperationType.READ,
        )
        e = GovernanceEvent(
            event_id="evt-a-003",
            event_type=EventType.SCOPE_ASSERTED,
            session_id="sess-a-001",
            event_sequence_number=3,
            previous_event_id="evt-a-002",
            agent_id="reporting-agent-v1",
            deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="2026-04-14T09:00:00.412Z",
            primitive=PrimitiveType.SCOPE,
            payload=p,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        assert e.event_type == EventType.SCOPE_ASSERTED

    def test_context_snapshot(self):
        p = ContextSnapshotPayload(
            data_classifications=["internal"],
            classification_source=ClassificationSource.vendor,
            provenance=["crm"],
            retention_flags=["may-persist"],
            context_size_tokens=1200,
        )
        e = GovernanceEvent(
            event_id="evt-a-004",
            event_type=EventType.CONTEXT_SNAPSHOT,
            session_id="sess-a-001",
            event_sequence_number=4,
            previous_event_id="evt-a-003",
            agent_id="reporting-agent-v1",
            deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="2026-04-14T09:00:00.891Z",
            primitive=PrimitiveType.CONTEXT,
            payload=p,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        assert e.event_type == EventType.CONTEXT_SNAPSHOT

    def test_memory_write_attempt(self):
        p = MemoryWriteAttemptPayload(
            write_type=WriteType.explicit_persist,
            detection_mechanism=None,
            target_store="report_db",
            write_classification="internal",
            write_size_tokens=800,
            retention_requested="30_days",
        )
        e = GovernanceEvent(
            event_id="evt-a-005",
            event_type=EventType.MEMORY_WRITE_ATTEMPT,
            session_id="sess-a-001",
            event_sequence_number=5,
            previous_event_id="evt-a-004",
            agent_id="reporting-agent-v1",
            deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="2026-04-14T09:00:01.204Z",
            primitive=PrimitiveType.MEMORY,
            payload=p,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        assert e.event_type == EventType.MEMORY_WRITE_ATTEMPT

    def test_governance_error(self):
        p = GovernanceErrorPayload(
            error_type=ErrorType.SCHEMA_VIOLATION,
            severity=Severity.warning,
            intercept_stage=InterceptStage.SCOPE,
            failure_reason="Test error",
            agent_continued=True,
        )
        e = GovernanceEvent(
            event_id="err-001",
            event_type=EventType.GOVERNANCE_ERROR,
            session_id="sess-x-001",
            event_sequence_number=1,
            previous_event_id=None,
            agent_id="test-agent",
            deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="2026-04-14T09:00:00.000Z",
            primitive=PrimitiveType.SYSTEM,
            payload=p,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        assert e.event_type == EventType.GOVERNANCE_ERROR
        assert e.payload.agent_continued is True


class TestGoldenTraceSchemaMatch:
    """Verify that every event in golden trace Flow A and Flow B can be
    represented by the schema without information loss."""

    def _check_event(self, raw: dict) -> None:
        # Core envelope fields must round-trip
        assert "event_id" in raw
        assert "event_type" in raw
        assert "session_id" in raw
        assert "event_sequence_number" in raw
        assert "payload" in raw
        assert "advisory_flags" in raw
        assert "policy_violations" in raw
        assert "pass_through" in raw

    def test_flow_a_schema(self):
        events = _load("golden_trace_flow_a.json")
        assert len(events) == 5
        for e in events:
            self._check_event(e)
        types = [e["event_type"] for e in events]
        assert types == [
            "AGENT_REGISTERED",
            "INTENT_DECLARED",
            "SCOPE_ASSERTED",
            "CONTEXT_SNAPSHOT",
            "MEMORY_WRITE_ATTEMPT",
        ]
        # All clean
        for e in events:
            assert e["advisory_flags"] == []
            assert e["policy_violations"] == []
            assert e["simulated_consequence"] is None

    def test_flow_b_schema(self):
        events = _load("golden_trace_flow_b.json")
        assert len(events) == 6
        for e in events:
            self._check_event(e)
        # All pass_through = True
        for e in events:
            assert e["pass_through"] is True
        # Flags present on every event
        assert events[0]["advisory_flags"] == ["AGENT_UNREGISTERED"]
        assert events[1]["advisory_flags"] == ["INTENT_MISSING"]
        assert set(events[2]["advisory_flags"]) == {"SCOPE_OPERATION_UNEXPECTED", "SCOPE_INTENT_MISMATCH"}
        assert events[3]["advisory_flags"] == ["CONTEXT_UNCLASSIFIED"]
        assert events[4]["advisory_flags"] == ["SENSITIVITY_ESCALATION"]
        assert set(events[5]["advisory_flags"]) == {"MEMORY_WRITE_UNCLASSIFIED", "MEMORY_WRITE_CANDIDATE"}


# ---------------------------------------------------------------------------
# v0.2.3 Track 2 — ContextSnapshotPayload token-field serialization
# ---------------------------------------------------------------------------


class TestTokenFieldSerialization:
    """The central serialization rule (per plan §1.3): None omitted,
    0 preserved. Existing required / list fields unchanged."""

    def _payload_dict(self, **token_kwargs):
        from sentience_governor.schema.events import (
            ClassificationSource,
            ContextSnapshotPayload,
        )
        payload = ContextSnapshotPayload(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=["test"],
            retention_flags=[],
            context_size_tokens=42,
            **token_kwargs,
        )
        return payload.model_dump()

    def test_none_token_fields_omitted(self):
        d = self._payload_dict()
        assert "llm_prompt_tokens" not in d
        assert "llm_completion_tokens" not in d
        assert "llm_cached_read_tokens" not in d
        assert "llm_cached_write_tokens" not in d
        assert "llm_reasoning_tokens" not in d
        assert "model_identifier" not in d
        assert "provider" not in d
        assert "llm_turn_id" not in d

    def test_zero_token_fields_preserved(self):
        """Zero is a real measurement. Must NOT be omitted."""
        d = self._payload_dict(llm_prompt_tokens=0, llm_completion_tokens=0)
        assert d["llm_prompt_tokens"] == 0
        assert d["llm_completion_tokens"] == 0

    def test_populated_fields_preserved(self):
        d = self._payload_dict(
            llm_prompt_tokens=100,
            llm_completion_tokens=50,
            model_identifier="claude-sonnet-4-5",
            provider="anthropic",
            llm_turn_id="abc123",
        )
        assert d["llm_prompt_tokens"] == 100
        assert d["llm_completion_tokens"] == 50
        assert d["model_identifier"] == "claude-sonnet-4-5"
        assert d["provider"] == "anthropic"
        assert d["llm_turn_id"] == "abc123"

    def test_existing_fields_unchanged_by_token_addition(self):
        """Backward compat hard gate: existing fields keep their shape."""
        d = self._payload_dict()
        assert d["data_classifications"] == []
        assert d["classification_source"] == "unclassified"
        assert d["provenance"] == ["test"]
        assert d["retention_flags"] == []
        assert d["context_size_tokens"] == 42

    def test_partial_population(self):
        """Only some fields populated — others omitted, populated kept."""
        d = self._payload_dict(llm_prompt_tokens=100)
        assert d["llm_prompt_tokens"] == 100
        # Other token fields absent.
        assert "llm_completion_tokens" not in d
        assert "model_identifier" not in d
