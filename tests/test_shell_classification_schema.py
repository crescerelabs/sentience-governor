"""v0.3.2 CP4: classification models on the event schema.

``ClassifiedEffect``, ``ClassifiedSegment`` and ``OperationClassification``
are additive; ``ScopeAssertedPayload.operation_classification`` is optional
and None-omitted, so every existing SCOPE_ASSERTED event serializes exactly
as before. The legacy ``operation_type`` is untouched. Vocabularies are
closed ``Literal`` types and rejected at construction.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from sentience_governor.schema.events import (
    ClassifiedEffect,
    ClassifiedSegment,
    DeploymentMode,
    EventType,
    GovernanceEvent,
    OperationClassification,
    OperationType,
    PrimitiveType,
    ScopeAssertedPayload,
)


def _classification() -> OperationClassification:
    return OperationClassification(
        classifier="shell_rules",
        classifier_version=1,
        complete=True,
        destructive=None,
        segments=[
            ClassifiedSegment(
                executable="curl",
                effects=[
                    ClassifiedEffect(domain="network", action="read", destructive=False),
                    ClassifiedEffect(domain="filesystem", action="modify", destructive=None),
                ],
            ),
            ClassifiedSegment(
                executable="aws",
                subcommand="ec2 describe-instances",
                effects=[ClassifiedEffect(domain="cloud_infrastructure", action="read", destructive=False)],
            ),
        ],
    )


def _scope(**kw) -> ScopeAssertedPayload:
    return ScopeAssertedPayload(
        tool_id="Bash", asserted_permissions=["execute"], target_system="shell",
        operation_type=OperationType.EXECUTE, **kw,
    )


class TestModels:
    def test_round_trip(self):
        obj = _classification()
        dumped = obj.model_dump()
        assert dumped == {
            "classifier": "shell_rules", "classifier_version": 1, "complete": True, "destructive": None,
            "segments": [
                {"executable": "curl", "effects": [
                    {"domain": "network", "action": "read", "destructive": False},
                    {"domain": "filesystem", "action": "modify", "destructive": None}]},
                {"executable": "aws", "subcommand": "ec2 describe-instances", "effects": [
                    {"domain": "cloud_infrastructure", "action": "read", "destructive": False}]},
            ],
        }
        assert OperationClassification.model_validate(json.loads(json.dumps(dumped))) == obj

    def test_subcommand_none_omitted_but_accepted_on_input(self):
        seg = ClassifiedSegment(executable="ls")
        assert "subcommand" not in seg.model_dump()
        assert ClassifiedSegment.model_validate({"executable": "ls", "subcommand": None, "effects": []}).subcommand is None

    def test_destructive_is_tri_state(self):
        for v in (True, False, None):
            assert ClassifiedEffect(domain="filesystem", action="modify", destructive=v).destructive is v
        assert ClassifiedEffect(domain="filesystem", action="modify").destructive is None
        assert OperationClassification(classifier="x", classifier_version=1, complete=False).destructive is None

    @pytest.mark.parametrize("bad", [{"domain": "cloud", "action": "read"}, {"domain": "filesystem", "action": "write"},
                                     {"domain": "", "action": "read"}, {"domain": "FILESYSTEM", "action": "read"}])
    def test_vocabularies_are_closed(self, bad):
        with pytest.raises(ValidationError):
            ClassifiedEffect(**bad)

    def test_no_aggregate_fields_exist(self):
        assert "domain" not in OperationClassification.model_fields
        assert "action" not in OperationClassification.model_fields
        assert "domain" not in ClassifiedSegment.model_fields

    def test_effects_default_empty_and_ordered(self):
        seg = ClassifiedSegment(executable="x")
        assert seg.effects == []
        obj = _classification()
        assert [e.domain for e in obj.segments[0].effects] == ["network", "filesystem"]


class TestScopePayload:
    def test_omitted_when_absent_so_existing_events_are_unchanged(self):
        dumped = _scope().model_dump()
        assert "operation_classification" not in dumped
        assert "tool_use_id" not in dumped
        assert dumped["operation_type"] == OperationType.EXECUTE
        assert list(dumped) == ["tool_id", "asserted_permissions", "target_system", "operation_type"]

    def test_present_when_set_and_legacy_fields_untouched(self):
        p = _scope(operation_classification=_classification(), tool_use_id="toolu_1")
        dumped = p.model_dump()
        assert dumped["operation_type"] == OperationType.EXECUTE
        assert dumped["target_system"] == "shell"
        assert dumped["operation_classification"]["segments"][1]["subcommand"] == "ec2 describe-instances"
        assert dumped["tool_use_id"] == "toolu_1"

    def test_event_envelope_serializes_and_parses(self):
        p = _scope(operation_classification=_classification())
        e = GovernanceEvent(
            event_id="evt-1", event_type=EventType.SCOPE_ASSERTED, session_id="s", event_sequence_number=3,
            previous_event_id="evt-0", agent_id="a", deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="2026-09-14T00:00:00.000Z", primitive=PrimitiveType.SCOPE, payload=p,
            advisory_flags=[], policy_violations=[], simulated_consequence=None, pass_through=True,
        )
        line = json.dumps(e.to_dict())
        back = json.loads(line)
        assert back["payload"]["operation_classification"]["complete"] is True
        assert back["payload"]["operation_type"] == "EXECUTE"
        assert "64" not in back["payload"]["operation_classification"]["classifier"]
        parsed = ScopeAssertedPayload.model_validate(back["payload"])
        assert parsed.operation_classification == _classification()

    def test_absent_object_parses_from_legacy_payload(self):
        parsed = ScopeAssertedPayload.model_validate({
            "tool_id": "Bash", "asserted_permissions": ["execute"], "target_system": "shell", "operation_type": "EXECUTE",
        })
        assert parsed.operation_classification is None
        assert "operation_classification" not in parsed.model_dump()
