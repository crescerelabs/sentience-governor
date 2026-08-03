"""Step 5 acceptance: Sink Writer failure modes and escalation."""

import io
import json
import tempfile
from pathlib import Path

import pytest

from sentience_governor.schema.events import (
    DeploymentMode,
    EventType,
    GovernanceEvent,
    GovernanceErrorPayload,
    ErrorType,
    PrimitiveType,
    Severity,
    AgentRegisteredPayload,
)
from sentience_governor.sink.writer import FileSink, SinkWriter, StdoutSink


def _make_event(session_id="s1", seq=1) -> GovernanceEvent:
    payload = AgentRegisteredPayload(
        agent_id="test-agent",
        agent_version="1.0",
        vendor_id="test-vendor",
        deployment_mode=DeploymentMode.vendor_managed,
        declared_capabilities=[],
        owner_claim=None,
    )
    return GovernanceEvent(
        event_id=f"evt-{seq}",
        event_type=EventType.AGENT_REGISTERED,
        session_id=session_id,
        event_sequence_number=seq,
        previous_event_id=None,
        agent_id="test-agent",
        deployment_mode=DeploymentMode.vendor_managed,
        timestamp_utc="2026-04-14T00:00:00.000Z",
        primitive=PrimitiveType.REGISTRATION,
        payload=payload,
        advisory_flags=[],
        policy_violations=[],
        simulated_consequence=None,
        pass_through=True,
    )


class _FailingSink:
    def write(self, event):
        return False


class TestFileSink:
    def test_file_sink_appends(self, tmp_path):
        path = str(tmp_path / "events.ndjson")
        sink = FileSink(path)
        event = _make_event()
        assert sink.write(event) is True
        assert sink.write(event) is True
        lines = Path(path).read_text().strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert parsed["event_id"] == "evt-1"


class TestSinkWriterFailureEscalation:
    def test_single_failure_warning(self, capsys):
        writer = SinkWriter(_FailingSink())
        writer.write(_make_event(seq=1), "s1")
        captured = capsys.readouterr()
        # GOVERNANCE_ERROR emitted to stdout
        lines = [l for l in captured.out.strip().split("\n") if l]
        assert len(lines) >= 1
        err = json.loads(lines[-1])
        assert err["event_type"] == "GOVERNANCE_ERROR"
        payload = err["payload"]
        assert payload["severity"] == "warning"

    def test_three_consecutive_degraded(self, capsys):
        writer = SinkWriter(_FailingSink())
        for i in range(3):
            writer.write(_make_event(seq=i + 1), "s1")
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split("\n") if l]
        # Last error should be degraded
        last = json.loads(lines[-1])
        assert last["payload"]["severity"] == "degraded"

    def test_agent_never_blocked(self):
        """SinkWriter must never raise regardless of sink failures."""
        writer = SinkWriter(_FailingSink())
        for i in range(10):
            # Must not raise
            writer.write(_make_event(seq=i + 1), "s1")

    def test_governance_error_always_stdout(self, capsys):
        """GOVERNANCE_ERROR goes to stdout even with a failing sink."""
        writer = SinkWriter(_FailingSink())
        err_payload = GovernanceErrorPayload(
            error_type=ErrorType.SINK_UNAVAILABLE,
            severity=Severity.critical,
            failure_reason="test",
            agent_continued=True,
        )
        err_event = GovernanceEvent(
            event_id="gov-err-1",
            event_type=EventType.GOVERNANCE_ERROR,
            session_id="s1",
            event_sequence_number=1,
            previous_event_id=None,
            agent_id="test-agent",
            deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="2026-04-14T00:00:00.000Z",
            primitive=PrimitiveType.SYSTEM,
            payload=err_payload,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        writer.write(err_event, "s1")
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split("\n") if l]
        assert len(lines) >= 1
        parsed = json.loads(lines[0])
        assert parsed["event_type"] == "GOVERNANCE_ERROR"
