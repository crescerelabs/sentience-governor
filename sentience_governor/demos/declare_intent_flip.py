"""declare_intent BEFORE/AFTER showcase (v0.3.0).

Demonstrates the capture-side POL-001 flip that ``declare_intent`` unlocks.
Without a declaration, mutating tool calls fire POL-001 (structural noise);
with a mid-session ``declare_intent`` whose scope covers the target,
subsequent matching activity stops firing POL-001, while pre-declaration
events keep theirs (non-retroactive).

Honesty of construction:

- ``SCOPE_ASSERTED.policy_violations`` come from the REAL capture-time
  evaluator (:class:`EventBuilder`, the same code the Claude Code hook
  drives): the flip is produced by the declaration setting the intent
  baseline, NOT hand-authored.
- ``CONTEXT_SNAPSHOT`` token counts are synthetic demo data, IDENTICAL in
  the before and after runs, so the undeclared-spend difference is driven
  purely by the POL-001 flip, never by different numbers.
- The SAME analyzer (:func:`compute_undeclared_intent_spend`) runs on both
  traces: no analyzer change, no retrospective cleanup, and pre-declaration
  events are never rewritten.

The cross-process hook rehydration that makes this work in the live,
multi-process hook is validated separately by the CP5 tests; this showcase
runs the evaluator in-process to stay deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from sentience_governor.analyze.undeclared_intent import (
    compute_undeclared_intent_spend,
)
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile.loader import GovernanceProfile
from sentience_governor.schema.events import (
    DeploymentMode,
    IntentConfidence,
    IntentSource,
    OperationType,
)
from sentience_governor.session_manager.manager import SessionManager

BEFORE_SESSION_ID = "demo-declare-intent-before"
AFTER_SESSION_ID = "demo-declare-intent-after"
OBJECTIVE = "edit the project's source files"
SCOPE: List[str] = ["filesystem"]

_AGENT_ID = "demo-agent"
# Per-turn synthetic token counts (prompt, completion). IDENTICAL across the
# before and after runs so the undeclared-spend delta is the POL-001 flip.
_TURN_TOKENS = [(1200, 240), (900, 180), (1100, 220)]


@dataclass
class FlipResult:
    """Structured before/after outcome for the CLI and the tests."""

    before_events: List[Dict[str, Any]]
    after_events: List[Dict[str, Any]]
    before_scope_pol001: List[bool]  # per mutating turn
    after_scope_pol001: List[bool]  # per mutating turn (turn 0 = pre-declaration)
    before_spend: Dict[str, Any]
    after_spend: Dict[str, Any]


def _new_builder(session_id: str) -> EventBuilder:
    session_manager = SessionManager()
    cache = InProcessCache()
    # Explicit DEFAULT posture (demand_at=session_start) so the showcase is
    # deterministic regardless of any ~/.sentience/profile.yaml on the box.
    session_manager.session_start(
        session_id=session_id,
        agent_id=_AGENT_ID,
        initial_sequence=0,
        initial_last_event_id=None,
        profile=GovernanceProfile.defaults(),
    )
    cache.init_session(session_id)
    return EventBuilder(
        session_manager=session_manager,
        cache=cache,
        agent_id=_AGENT_ID,
        session_id=session_id,
        deployment_mode=DeploymentMode.vendor_managed,
    )


def _write_scope(builder: EventBuilder) -> Dict[str, Any]:
    """One mutating Write tool call, evaluated by the real builder."""
    event = builder.build_scope_asserted(
        tool_id="Write",
        asserted_permissions=["write"],
        target_system="filesystem",
        operation_type=OperationType.WRITE,
        authorization_claim=None,
        tool_use_id=None,
    )
    return event.to_dict()


def _context_snapshot(
    session_id: str, turn_id: str, prompt: int, completion: int
) -> Dict[str, Any]:
    return {
        "event_type": "CONTEXT_SNAPSHOT",
        "session_id": session_id,
        "advisory_flags": [],
        "policy_violations": [],
        "payload": {
            "llm_turn_id": turn_id,
            "llm_prompt_tokens": prompt,
            "llm_completion_tokens": completion,
            "llm_cached_read_tokens": 0,
            "llm_cached_write_tokens": 0,
        },
    }


def _has_pol001(scope_event: Dict[str, Any]) -> bool:
    return "POL-001" in scope_event.get("policy_violations", [])


def build_before_events() -> List[Dict[str, Any]]:
    """BEFORE: three mutating turns, no declaration. Every turn fires POL-001."""
    builder = _new_builder(BEFORE_SESSION_ID)
    events: List[Dict[str, Any]] = []
    for i, (prompt, completion) in enumerate(_TURN_TOKENS, start=1):
        events.append(_write_scope(builder))
        events.append(
            _context_snapshot(BEFORE_SESSION_ID, f"turn-{i}", prompt, completion)
        )
    return events


def build_after_events() -> List[Dict[str, Any]]:
    """AFTER: same three turns, but declare_intent lands after turn 1. Turn 1
    (pre-declaration) keeps POL-001; turns 2-3 (post, matching scope) do not."""
    builder = _new_builder(AFTER_SESSION_ID)
    events: List[Dict[str, Any]] = []

    # Turn 1 — pre-declaration mutating write (POL-001 fires).
    p, c = _TURN_TOKENS[0]
    events.append(_write_scope(builder))
    events.append(_context_snapshot(AFTER_SESSION_ID, "turn-1", p, c))

    # The declaration (server-written provenance in the live path; here the
    # same build_intent_declared the hook uses, with the same inferred/
    # inferred_low classification declare_intent applies).
    declaration = builder.build_intent_declared(
        stated_objective=OBJECTIVE,
        intent_source=IntentSource.inferred,
        intent_confidence=IntentConfidence.inferred_low,
        authorization_claim=None,
        session_scope_hint=list(SCOPE),
    )
    events.append(declaration.to_dict())

    # Turns 2-3 — post-declaration matching writes (POL-001 suppressed).
    for i, (prompt, completion) in enumerate(_TURN_TOKENS[1:], start=2):
        events.append(_write_scope(builder))
        events.append(
            _context_snapshot(AFTER_SESSION_ID, f"turn-{i}", prompt, completion)
        )
    return events


def run_flip_demo() -> FlipResult:
    """Build both traces and run the (unchanged) analyzer on each."""
    before = build_before_events()
    after = build_after_events()
    before_scopes = [e for e in before if e["event_type"] == "SCOPE_ASSERTED"]
    after_scopes = [e for e in after if e["event_type"] == "SCOPE_ASSERTED"]
    return FlipResult(
        before_events=before,
        after_events=after,
        before_scope_pol001=[_has_pol001(e) for e in before_scopes],
        after_scope_pol001=[_has_pol001(e) for e in after_scopes],
        before_spend=compute_undeclared_intent_spend(before),
        after_spend=compute_undeclared_intent_spend(after),
    )
