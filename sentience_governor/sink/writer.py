"""Sink Writer — synchronous, no-buffering, fail-open event emission.

Sinks
-----
* StdoutSink  — default, no configuration required.
* FileSink    — absolute path, append mode, newline-delimited JSON.
* HttpLocalSink — single POST per event, 500 ms timeout, local network only.

Failure semantics
-----------------
* Writes are synchronous.  No buffering.  No retry.
* On failure: drop event, emit GOVERNANCE_ERROR to stdout.
* Severity escalation per session:
    1 failure  → warning
    3 consecutive → degraded
    remainder of session → critical
* GOVERNANCE_ERROR always goes to stdout regardless of configured sink.
* Fail-open: agent execution is never blocked.
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from sentience_governor.schema.events import (
    ErrorType,
    EventType,
    GovernanceEvent,
    GovernanceErrorPayload,
    PrimitiveType,
    Severity,
)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class SinkBase(ABC):
    @abstractmethod
    def write(self, event: GovernanceEvent) -> bool:
        """Write event.  Return True on success, False on failure."""


# ---------------------------------------------------------------------------
# Stdout sink
# ---------------------------------------------------------------------------

class StdoutSink(SinkBase):
    def write(self, event: GovernanceEvent) -> bool:
        try:
            print(json.dumps(event.to_dict()), flush=True)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# File sink
# ---------------------------------------------------------------------------

class FileSink(SinkBase):
    def __init__(self, path: str) -> None:
        self._path = path

    def write(self, event: GovernanceEvent) -> bool:
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict()) + "\n")
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Local HTTP sink
# ---------------------------------------------------------------------------

class HttpLocalSink(SinkBase):
    _TIMEOUT_SECONDS = 0.5

    def __init__(self, url: str) -> None:
        self._url = url

    def write(self, event: GovernanceEvent) -> bool:
        try:
            body = json.dumps(event.to_dict()).encode("utf-8")
            req = urllib.request.Request(
                self._url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self._TIMEOUT_SECONDS) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False


# ---------------------------------------------------------------------------
# SinkWriter — orchestrates sink selection and failure escalation
# ---------------------------------------------------------------------------

class SinkWriter:
    """Writes governance events to the configured sink with fail-open semantics.

    GOVERNANCE_ERROR events always go to stdout regardless of configured sink.
    """

    def __init__(self, sink: SinkBase) -> None:
        self._sink = sink
        self._stdout = StdoutSink()
        # Per-session failure tracking: session_id → consecutive_failures
        self._consecutive_failures: dict[str, int] = {}

    def write(self, event: GovernanceEvent, session_id: str) -> None:
        """Write event to configured sink.  Never raises; never blocks agent."""
        # GOVERNANCE_ERROR always goes to stdout
        if event.event_type == EventType.GOVERNANCE_ERROR:
            self._stdout.write(event)
            return

        success = self._sink.write(event)
        if success:
            self._consecutive_failures[session_id] = 0
            return

        # Failure path
        failures = self._consecutive_failures.get(session_id, 0) + 1
        self._consecutive_failures[session_id] = failures

        severity = self._escalate_severity(failures)
        self._emit_sink_error(
            session_id=session_id,
            agent_id=event.agent_id,
            severity=severity,
            reason=f"Sink write failed for event {event.event_id} (consecutive={failures})",
        )

    def session_closed(self, session_id: str, agent_id: str) -> None:
        """Call when session closes; emit critical GOVERNANCE_ERROR if still failing."""
        failures = self._consecutive_failures.pop(session_id, 0)
        if failures >= 3:
            self._emit_sink_error(
                session_id=session_id,
                agent_id=agent_id,
                severity=Severity.critical,
                reason="Sink unreachable for remainder of session.",
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _escalate_severity(consecutive: int) -> Severity:
        if consecutive >= 3:
            return Severity.degraded
        return Severity.warning

    def _emit_sink_error(
        self,
        session_id: str,
        agent_id: str,
        severity: Severity,
        reason: str,
    ) -> None:
        payload = GovernanceErrorPayload(
            error_type=ErrorType.SINK_UNAVAILABLE,
            severity=severity,
            failure_reason=reason,
            agent_continued=True,
        )
        from sentience_governor.schema.events import DeploymentMode
        err_event = GovernanceEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.GOVERNANCE_ERROR,
            session_id=session_id,
            event_sequence_number=0,
            previous_event_id=None,
            agent_id=agent_id,
            deployment_mode=DeploymentMode.vendor_managed,
            timestamp_utc="",
            primitive=PrimitiveType.SYSTEM,
            payload=payload,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        self._stdout.write(err_event)
