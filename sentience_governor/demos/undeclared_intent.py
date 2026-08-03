"""Synthetic undeclared-intent demo session.

A 4-turn session where the agent declares one objective and then drifts
into an undeclared Slack write (turn 3), producing a POL-001 violation
and ~20.7% undeclared compute. Used by `sentience demo undeclared-intent`
and by examples/v024_undeclared_intent_demo.py (single source of truth).
"""

from __future__ import annotations

from typing import Any, Dict, List

SESSION_ID = "demo-v024-mixed-intent-0001"


def _intent_declared(objective: str) -> Dict[str, Any]:
    return {
        "event_type": "INTENT_DECLARED",
        "session_id": SESSION_ID,
        "advisory_flags": [],
        "policy_violations": [],
        "payload": {"stated_objective": objective},
    }


def _scope_asserted(
    tool_id: str,
    *,
    advisory_flags: List[str] | None = None,
    policy_violations: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "event_type": "SCOPE_ASSERTED",
        "session_id": SESSION_ID,
        "advisory_flags": list(advisory_flags or []),
        "policy_violations": list(policy_violations or []),
        "payload": {"tool_id": tool_id},
    }


def _context_snapshot(
    turn_id: str,
    *,
    prompt: int,
    completion: int,
    cached_read: int = 0,
    cached_write: int = 0,
) -> Dict[str, Any]:
    return {
        "event_type": "CONTEXT_SNAPSHOT",
        "session_id": SESSION_ID,
        "advisory_flags": [],
        "policy_violations": [],
        "payload": {
            "llm_turn_id": turn_id,
            "llm_prompt_tokens": prompt,
            "llm_completion_tokens": completion,
            "llm_cached_read_tokens": cached_read,
            "llm_cached_write_tokens": cached_write,
        },
    }


def build_session_events() -> List[Dict[str, Any]]:
    """Return a synthesized 4-turn session with one undeclared turn."""
    return [
        _intent_declared("export Q3 revenue summary as PDF"),
        # Turn 1 — declared: read invoices.
        _scope_asserted("crm.list_invoices"),
        _context_snapshot("turn-1", prompt=1200, completion=240),
        # Turn 2 — declared: format the summary.
        _scope_asserted("template.render"),
        _context_snapshot("turn-2", prompt=900, completion=180),
        # Turn 3 — UNDECLARED: agent drifts and writes to Slack.
        # The MCP wrapper would emit POL-001 here — write op outside
        # the declared objective scope.
        _scope_asserted(
            "slack.write_message",
            advisory_flags=["INTENT_MISSING"],
            policy_violations=["POL-001"],
        ),
        _context_snapshot("turn-3", prompt=850, completion=150),
        # Turn 4 — declared: write the PDF file (the actual objective).
        _scope_asserted("fs.write"),
        _context_snapshot("turn-4", prompt=1100, completion=220),
    ]
