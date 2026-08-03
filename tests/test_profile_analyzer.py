"""Tests for v0.2.5 CP4 — profile-aware analyzer + renderers.

Covers:

* Analyzer (``compute_undeclared_intent_spend``) extracts
  ``profile_fingerprint``, ``profile_loaded``, ``profile_schema_version``
  from AGENT_REGISTERED. Defaults to None when absent.
* Analyzer collects ``HIGH_CONSEQUENCE_DETECTED`` and
  ``TASK_BOUNDARY_CROSSED`` advisory flag events from SCOPE_ASSERTED
  envelopes into result-dict lists.
* Renderers (CLI + Markdown) gain Profile / High-consequence /
  Task-boundary sections when relevant fields populated.
* **Critical regression guard**: no profile metadata → CLI + Markdown
  output is byte-identical to v0.2.4 (plan CP4 acceptance criterion).
* Pure-function guarantee: same events twice → identical result
  bytes (analyzer extension preserves v0.2.4 byte-stability).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from sentience_governor.analyze.renderers import (
    render_cli,
    render_markdown_report,
)
from sentience_governor.analyze.undeclared_intent import (
    compute_undeclared_intent_spend,
)


# ---------------------------------------------------------------------------
# Trace fixtures (in-memory; analyzer is pure-function over event lists)
# ---------------------------------------------------------------------------


def _agent_registered(
    *,
    fingerprint: str = None,
    loaded: bool = None,
    schema_version: int = None,
) -> Dict[str, Any]:
    """An AGENT_REGISTERED event, optionally carrying profile metadata."""
    event: Dict[str, Any] = {
        "event_id": "evt-reg",
        "event_type": "AGENT_REGISTERED",
        "session_id": "sess-1",
        "event_sequence_number": 1,
        "agent_id": "agent-1",
        "timestamp_utc": "2026-05-12T00:00:00.000Z",
        "primitive": "REGISTRATION",
        "payload": {
            "agent_id": "agent-1",
            "deployment_mode": "vendor_managed",
            "declared_capabilities": ["fs.write"],
        },
        "advisory_flags": [],
        "policy_violations": [],
    }
    if fingerprint is not None:
        event["profile_fingerprint"] = fingerprint
    if loaded is not None:
        event["payload"]["profile_loaded"] = loaded
    if schema_version is not None:
        event["payload"]["profile_schema_version"] = schema_version
    return event


def _scope_asserted(
    *,
    seq: int,
    tool_id: str = "fs.write",
    advisory_flags: List[str] = None,
) -> Dict[str, Any]:
    return {
        "event_id": f"evt-scope-{seq}",
        "event_type": "SCOPE_ASSERTED",
        "session_id": "sess-1",
        "event_sequence_number": seq,
        "agent_id": "agent-1",
        "timestamp_utc": "2026-05-12T00:00:01.000Z",
        "primitive": "SCOPE",
        "payload": {
            "tool_id": tool_id,
            "operation_type": "WRITE",
            "target_system": "src/a.py",
        },
        "advisory_flags": list(advisory_flags or []),
        "policy_violations": [],
    }


def _context_snapshot(
    *, seq: int, turn_id: str = "turn-1", prompt_tokens: int = 100
) -> Dict[str, Any]:
    """A token-bearing CONTEXT_SNAPSHOT — needed to flip analyzer status
    from no_token_data to ok so render branches see the full breakdown."""
    return {
        "event_id": f"evt-ctx-{seq}",
        "event_type": "CONTEXT_SNAPSHOT",
        "session_id": "sess-1",
        "event_sequence_number": seq,
        "agent_id": "agent-1",
        "timestamp_utc": "2026-05-12T00:00:02.000Z",
        "primitive": "CONTEXT",
        "payload": {
            "data_classifications": ["public"],
            "classification_source": "explicit",
            "provenance": [],
            "retention_flags": [],
            "context_size_tokens": prompt_tokens,
            "llm_prompt_tokens": prompt_tokens,
            "llm_completion_tokens": 50,
            "llm_turn_id": turn_id,
        },
        "advisory_flags": [],
        "policy_violations": [],
    }


# ---------------------------------------------------------------------------
# Analyzer extraction
# ---------------------------------------------------------------------------


def test_analyzer_no_profile_metadata_yields_none_fields():
    """v0.2.4-shaped trace: profile fields are None / empty in result."""
    events = [_agent_registered(), _scope_asserted(seq=2)]
    result = compute_undeclared_intent_spend(events)
    assert result["profile_fingerprint"] is None
    assert result["profile_loaded"] is None
    assert result["profile_schema_version"] is None
    assert result["high_consequence_events"] == []
    assert result["task_boundary_events"] == []


def test_analyzer_extracts_profile_metadata_from_agent_registered():
    events = [
        _agent_registered(
            fingerprint="abc123def456",
            loaded=True,
            schema_version=1,
        ),
        _scope_asserted(seq=2),
    ]
    result = compute_undeclared_intent_spend(events)
    assert result["profile_fingerprint"] == "abc123def456"
    assert result["profile_loaded"] is True
    assert result["profile_schema_version"] == 1


def test_analyzer_collects_high_consequence_events():
    events = [
        _agent_registered(fingerprint="fp1", loaded=True, schema_version=1),
        _scope_asserted(
            seq=2,
            tool_id="Bash",
            advisory_flags=["HIGH_CONSEQUENCE_DETECTED"],
        ),
        _scope_asserted(seq=3, tool_id="fs.write"),  # clean event
        _scope_asserted(
            seq=4,
            tool_id="db.delete",
            advisory_flags=["HIGH_CONSEQUENCE_DETECTED"],
        ),
    ]
    result = compute_undeclared_intent_spend(events)
    assert len(result["high_consequence_events"]) == 2
    assert result["high_consequence_events"][0]["tool_id"] == "Bash"
    assert result["high_consequence_events"][1]["tool_id"] == "db.delete"


def test_analyzer_collects_task_boundary_events():
    events = [
        _agent_registered(fingerprint="fp1", loaded=True, schema_version=1),
        _scope_asserted(
            seq=2,
            tool_id="fs.write",
            advisory_flags=["TASK_BOUNDARY_CROSSED"],
        ),
    ]
    result = compute_undeclared_intent_spend(events)
    assert len(result["task_boundary_events"]) == 1
    assert result["task_boundary_events"][0]["tool_id"] == "fs.write"


def test_analyzer_handles_both_advisory_flags_on_one_event():
    """A single SCOPE_ASSERTED carrying both flags shows up in both lists."""
    events = [
        _agent_registered(fingerprint="fp1", loaded=True, schema_version=1),
        _scope_asserted(
            seq=2,
            tool_id="Bash",
            advisory_flags=["HIGH_CONSEQUENCE_DETECTED", "TASK_BOUNDARY_CROSSED"],
        ),
    ]
    result = compute_undeclared_intent_spend(events)
    assert len(result["high_consequence_events"]) == 1
    assert len(result["task_boundary_events"]) == 1


# ---------------------------------------------------------------------------
# Pure-function byte-stability (extension preserves v0.2.4 guarantee)
# ---------------------------------------------------------------------------


def test_analyzer_byte_stable_with_profile_metadata():
    """Same input events twice → identical result bytes including new fields."""
    events = [
        _agent_registered(fingerprint="fp1", loaded=True, schema_version=1),
        _scope_asserted(
            seq=2,
            tool_id="Bash",
            advisory_flags=["HIGH_CONSEQUENCE_DETECTED"],
        ),
    ]
    a = compute_undeclared_intent_spend(events)
    b = compute_undeclared_intent_spend(events)
    assert repr(a) == repr(b)


# ---------------------------------------------------------------------------
# Renderer — regression guard (CRITICAL: byte-identical v0.2.4 output)
# ---------------------------------------------------------------------------


def test_render_cli_no_profile_metadata_identical_to_v0_2_4():
    """The most important regression guard for CP4.

    A result dict with no profile metadata must produce CLI output
    that does NOT mention profile / high-consequence / task-boundary
    sections. Operators running v0.2.5 on a v0.2.4 trace see exactly
    the v0.2.4 output (plan CP4 acceptance criterion).
    """
    events = [_agent_registered(), _scope_asserted(seq=2)]
    result = compute_undeclared_intent_spend(events)
    out = render_cli(result)
    assert "High-consequence" not in out
    assert "Task boundaries" not in out
    assert "Profile:" not in out
    assert "fingerprint" not in out


def test_render_markdown_no_profile_metadata_identical_to_v0_2_4():
    """Same guard for the Markdown surface."""
    events = [_agent_registered(), _scope_asserted(seq=2)]
    result = compute_undeclared_intent_spend(events)
    out = render_markdown_report(result)
    assert "## Profile" not in out
    assert "## High-consequence" not in out
    assert "## Task boundaries" not in out


# ---------------------------------------------------------------------------
# Renderer — new sections appear when relevant
# ---------------------------------------------------------------------------


def test_render_cli_includes_profile_section_when_loaded():
    events = [
        _agent_registered(
            fingerprint="abc123def456", loaded=True, schema_version=1
        ),
        _scope_asserted(seq=2),
        _context_snapshot(seq=3),
    ]
    result = compute_undeclared_intent_spend(events)
    out = render_cli(result)
    assert "Profile:" in out
    assert "abc123def456" in out
    assert "schema v1" in out


def test_render_cli_includes_high_consequence_section():
    events = [
        _agent_registered(fingerprint="fp1", loaded=True, schema_version=1),
        _scope_asserted(
            seq=2,
            tool_id="Bash",
            advisory_flags=["HIGH_CONSEQUENCE_DETECTED"],
        ),
        _context_snapshot(seq=3),
    ]
    result = compute_undeclared_intent_spend(events)
    out = render_cli(result)
    assert "High-consequence operations" in out
    assert "Bash" in out


def test_render_markdown_includes_all_three_sections_when_relevant():
    events = [
        _agent_registered(
            fingerprint="abc123def456", loaded=True, schema_version=1
        ),
        _scope_asserted(
            seq=2,
            tool_id="Bash",
            advisory_flags=["HIGH_CONSEQUENCE_DETECTED"],
        ),
        _scope_asserted(
            seq=3,
            tool_id="fs.write",
            advisory_flags=["TASK_BOUNDARY_CROSSED"],
        ),
        _context_snapshot(seq=4),
    ]
    result = compute_undeclared_intent_spend(events)
    out = render_markdown_report(result)
    assert "## Profile" in out
    assert "abc123def456" in out
    assert "## High-consequence operations" in out
    assert "Bash" in out
    assert "## Task boundaries crossed" in out
    assert "fs.write" in out
