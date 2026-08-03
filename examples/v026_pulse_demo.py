"""v0.2.6 pulse demo — three operator stories, byte-stable outputs.

Demonstrates the v0.2.6 adoption surface (`sentience pulse`) across
the three states an operator will actually encounter:

  1. **Clean session.** Well-behaved agent + permissive profile →
     no violations, no advisory flags. Confirms the recurring-loop
     value: the run-to-run evidence record is intact.

  2. **Missing-intent session.** Claude Code-style trace where the
     intent-declaration primitive isn't exposed → POL-001 fires on
     every mutating SCOPE_ASSERTED. Pulse explains why every turn
     surfaces as undeclared.

  3. **Mixed-violations session.** Tighter profile + multi-rule
     firings across multiple turns → pulse's per-rule prioritization
     signal becomes visible.

This script does steps 1-3 in-process: it loads each example
profile, synthesizes the matching trace (no live agent needed), runs
`compute_pulse`, and writes per-case::

  examples/showcase/v026-pulse/<case>/session.jsonl
  examples/showcase/v026-pulse/<case>/pulse_output.md

It also retrofits the v0.2.5 closed-loop showcase with a pulse
output (the v0.2.5 trace is a clean session in v0.2.6 terms — it
makes a useful cross-link from the older showcase)::

  examples/showcase/v025-closed-loop/pulse_output.md

All outputs are byte-stable across runs (synthesized traces pin
event_ids + timestamps; the analyzer + renderers are pure
functions). Tests in `tests/test_v026_pulse_demo.py` catch
unintended drift.

Run::

    python examples/v026_pulse_demo.py

Re-run any case through the live CLI::

    sentience pulse examples/showcase/v026-pulse/mixed_violations/session.jsonl
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from sentience_governor.analyze import compute_pulse, render_pulse_markdown
from sentience_governor.profile import GovernanceProfile

# ---------------------------------------------------------------------------
# Pinned identifiers — byte-stability requires fixed strings everywhere.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent
SHOWCASE_DIR = REPO_ROOT / "showcase" / "v026-pulse"
V025_SHOWCASE_DIR = REPO_ROOT / "showcase" / "v025-closed-loop"

CASES = ("clean", "missing_intent", "mixed_violations")

_AGENT_ID = "showcase-agent-v026"

# One frozen timestamp per case — keeps deterministic output but lets
# session-duration > 0 by giving each event the same instant. (The
# pulse session_duration_seconds is derived from event timestamps;
# all-same-timestamp produces duration=0, which is fine for fixture
# stability and matches the "synthesized in-memory" disclaimer.)
_TIMESTAMP = "2026-05-28T12:00:00.000Z"

# Pinned synthetic sync_prompt eligibility for the saved pulse_output.md
# files. The showcase wants to demonstrate the operator-facing surface
# *including* the sync-registration footer, so we attach the
# "not_registered" eligibility before rendering. (In the real CLI
# this is set by the CP6 handler from disk + env state; here we set
# it directly so the saved Markdown shows the footer.)
_SHOWCASE_SYNC_PROMPT = {"show": True, "reason": "not_registered"}


# ---------------------------------------------------------------------------
# Common event builder — mirrors v0.2.5 closed-loop demo for consistency.
# ---------------------------------------------------------------------------


def _event(
    *,
    session_id: str,
    event_id: str,
    event_type: str,
    sequence: int,
    primitive: str,
    payload: Dict[str, Any],
    fingerprint: str,
    advisory_flags: List[str] | None = None,
    policy_violations: List[str] | None = None,
    previous_event_id: str | None = None,
) -> Dict[str, Any]:
    """Build one governance event with all v0.2.5+ envelope fields."""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "session_id": session_id,
        "event_sequence_number": sequence,
        "previous_event_id": previous_event_id,
        "agent_id": _AGENT_ID,
        "deployment_mode": "vendor_managed",
        "timestamp_utc": _TIMESTAMP,
        "primitive": primitive,
        "payload": payload,
        "advisory_flags": list(advisory_flags or []),
        "policy_violations": list(policy_violations or []),
        "simulated_consequence": None,
        "pass_through": True,
        "profile_fingerprint": fingerprint,
    }


def _registration(
    *, session_id: str, fingerprint: str, profile: GovernanceProfile
) -> Dict[str, Any]:
    return _event(
        session_id=session_id,
        event_id="evt-reg",
        event_type="AGENT_REGISTERED",
        sequence=1,
        primitive="REGISTRATION",
        payload={
            "agent_id": _AGENT_ID,
            "agent_version": "0.2.6",
            "vendor_id": "example-co",
            "deployment_mode": "vendor_managed",
            "declared_capabilities": ["fs.read", "fs.write", "Bash"],
            "owner_claim": "operator@example.com",
            "policy_context": None,
            "profile_loaded": True,
            "profile_schema_version": profile.schema_version,
        },
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# Story 1 — Clean session
# ---------------------------------------------------------------------------


def _build_clean_events(profile: GovernanceProfile) -> List[Dict[str, Any]]:
    """A well-behaved agent under a permissive profile.

    Story:
      * Operator declares intent up front (read + write under
        src/auth/).
      * Turn 1 reads the existing module.
      * Turn 2 writes the same module.

    No POL-* violations fire. No advisory flags fire.
    """
    fp = profile.fingerprint()
    sid = "demo-v026-clean-0001"
    return [
        _registration(session_id=sid, fingerprint=fp, profile=profile),
        _event(
            session_id=sid, event_id="evt-intent",
            event_type="INTENT_DECLARED", sequence=2, primitive="INTENT",
            payload={
                "stated_objective": (
                    "refactor src/auth/middleware.py to add retry-on-401"
                ),
                "intent_source": "explicit",
                "intent_confidence": "explicit",
                "authorization_claim": "operator@example.com",
                "session_scope_hint": ["fs.read", "fs.write"],
            },
            fingerprint=fp, previous_event_id="evt-reg",
        ),
        _event(
            session_id=sid, event_id="evt-scope-1",
            event_type="SCOPE_ASSERTED", sequence=3, primitive="SCOPE",
            payload={
                "tool_id": "fs.read",
                "asserted_permissions": ["read"],
                "target_system": "src/auth/middleware.py",
                "operation_type": "READ",
            },
            fingerprint=fp, previous_event_id="evt-intent",
        ),
        _event(
            session_id=sid, event_id="evt-ctx-1",
            event_type="CONTEXT_SNAPSHOT", sequence=4, primitive="CONTEXT",
            payload={
                "data_classifications": ["internal"],
                "classification_source": "explicit",
                "provenance": ["src/auth/middleware.py"],
                "retention_flags": [],
                "context_size_tokens": 1200,
                "llm_prompt_tokens": 1200,
                "llm_completion_tokens": 240,
                "llm_turn_id": "turn-1",
            },
            fingerprint=fp, previous_event_id="evt-scope-1",
        ),
        _event(
            session_id=sid, event_id="evt-scope-2",
            event_type="SCOPE_ASSERTED", sequence=5, primitive="SCOPE",
            payload={
                "tool_id": "fs.write",
                "asserted_permissions": ["write"],
                "target_system": "src/auth/middleware.py",
                "operation_type": "WRITE",
            },
            fingerprint=fp, previous_event_id="evt-ctx-1",
        ),
        _event(
            session_id=sid, event_id="evt-ctx-2",
            event_type="CONTEXT_SNAPSHOT", sequence=6, primitive="CONTEXT",
            payload={
                "data_classifications": ["internal"],
                "classification_source": "explicit",
                "provenance": ["src/auth/middleware.py"],
                "retention_flags": [],
                "context_size_tokens": 1100,
                "llm_prompt_tokens": 1100,
                "llm_completion_tokens": 220,
                "llm_turn_id": "turn-2",
            },
            fingerprint=fp, previous_event_id="evt-scope-2",
        ),
    ]


# ---------------------------------------------------------------------------
# Story 2 — Missing-intent session (Claude Code-style today)
# ---------------------------------------------------------------------------


def _build_missing_intent_events(
    profile: GovernanceProfile,
) -> List[Dict[str, Any]]:
    """A Claude Code-style trace where intent is never declared.

    Story:
      * No INTENT_DECLARED event — the runtime surface doesn't expose
        intent today.
      * Every mutating SCOPE_ASSERTED carries POL-001 (intent missing
        before mutating op).
      * Three turns of mutating work; all of it shows up as
        undeclared at the session level.
    """
    fp = profile.fingerprint()
    sid = "demo-v026-missing-intent-0001"
    pol_001 = ["POL-001"]
    events: List[Dict[str, Any]] = [
        _registration(session_id=sid, fingerprint=fp, profile=profile),
    ]
    # Three mutating turns; each fires POL-001 on the SCOPE_ASSERTED
    # because no INTENT_DECLARED preceded the first write.
    targets = [
        ("src/auth/middleware.py", 1300, 260),
        ("src/auth/session.py", 1100, 220),
        ("src/api/handlers.py", 900, 180),
    ]
    seq = 2
    prev = "evt-reg"
    for i, (target, prompt_tokens, completion_tokens) in enumerate(
        targets, start=1
    ):
        scope_id = f"evt-scope-{i}"
        ctx_id = f"evt-ctx-{i}"
        events.append(
            _event(
                session_id=sid, event_id=scope_id,
                event_type="SCOPE_ASSERTED", sequence=seq,
                primitive="SCOPE",
                payload={
                    "tool_id": "fs.write",
                    "asserted_permissions": ["write"],
                    "target_system": target,
                    "operation_type": "WRITE",
                },
                fingerprint=fp,
                policy_violations=pol_001,
                previous_event_id=prev,
            )
        )
        seq += 1
        events.append(
            _event(
                session_id=sid, event_id=ctx_id,
                event_type="CONTEXT_SNAPSHOT", sequence=seq,
                primitive="CONTEXT",
                payload={
                    "data_classifications": ["internal"],
                    "classification_source": "explicit",
                    "provenance": [target],
                    "retention_flags": [],
                    "context_size_tokens": prompt_tokens,
                    "llm_prompt_tokens": prompt_tokens,
                    "llm_completion_tokens": completion_tokens,
                    "llm_turn_id": f"turn-{i}",
                },
                fingerprint=fp, previous_event_id=scope_id,
            )
        )
        seq += 1
        prev = ctx_id
    return events


# ---------------------------------------------------------------------------
# Story 3 — Mixed violations
# ---------------------------------------------------------------------------


def _build_mixed_violations_events(
    profile: GovernanceProfile,
) -> List[Dict[str, Any]]:
    """A session under a tight profile with multiple distinct rules
    firing across multiple turns.

    Story:
      * Turn 1 — fs.write with POL-001 (intent missing).
      * Turn 2 — CONTEXT_SNAPSHOT carries POL-003
        (CONTEXT_UNCLASSIFIED on this turn's snapshot).
      * Turn 3 — fs.write to a .env file. POL-005 fires on this
        turn's snapshot (sensitivity escalation as classification
        shifts from internal → confidential).
    """
    fp = profile.fingerprint()
    sid = "demo-v026-mixed-0001"
    return [
        _registration(session_id=sid, fingerprint=fp, profile=profile),
        # Turn 1 — POL-001 (intent missing, mutating op).
        _event(
            session_id=sid, event_id="evt-scope-1",
            event_type="SCOPE_ASSERTED", sequence=2, primitive="SCOPE",
            payload={
                "tool_id": "fs.write",
                "asserted_permissions": ["write"],
                "target_system": "src/api/handlers.py",
                "operation_type": "WRITE",
            },
            fingerprint=fp,
            policy_violations=["POL-001"],
            previous_event_id="evt-reg",
        ),
        _event(
            session_id=sid, event_id="evt-ctx-1",
            event_type="CONTEXT_SNAPSHOT", sequence=3, primitive="CONTEXT",
            payload={
                "data_classifications": ["internal"],
                "classification_source": "explicit",
                "provenance": ["src/api/handlers.py"],
                "retention_flags": [],
                "context_size_tokens": 1400,
                "llm_prompt_tokens": 1400,
                "llm_completion_tokens": 280,
                "llm_turn_id": "turn-1",
            },
            fingerprint=fp, previous_event_id="evt-scope-1",
        ),
        # Turn 2 — POL-003 (CONTEXT_UNCLASSIFIED on the same snapshot
        # — same-event attribution path per CP1 plan v3.6 spec).
        _event(
            session_id=sid, event_id="evt-scope-2",
            event_type="SCOPE_ASSERTED", sequence=4, primitive="SCOPE",
            payload={
                "tool_id": "fs.read",
                "asserted_permissions": ["read"],
                "target_system": "data/customers.csv",
                "operation_type": "READ",
            },
            fingerprint=fp, previous_event_id="evt-ctx-1",
        ),
        _event(
            session_id=sid, event_id="evt-ctx-2",
            event_type="CONTEXT_SNAPSHOT", sequence=5, primitive="CONTEXT",
            payload={
                "data_classifications": [],
                "classification_source": "inferred",
                "provenance": ["data/customers.csv"],
                "retention_flags": [],
                "context_size_tokens": 2000,
                "llm_prompt_tokens": 2000,
                "llm_completion_tokens": 400,
                "llm_turn_id": "turn-2",
            },
            fingerprint=fp,
            advisory_flags=["CONTEXT_UNCLASSIFIED"],
            policy_violations=["POL-003"],
            previous_event_id="evt-scope-2",
        ),
        # Turn 3 — POL-005 (sensitivity escalation on this snapshot).
        _event(
            session_id=sid, event_id="evt-scope-3",
            event_type="SCOPE_ASSERTED", sequence=6, primitive="SCOPE",
            payload={
                "tool_id": "fs.write",
                "asserted_permissions": ["write"],
                "target_system": ".env",
                "operation_type": "WRITE",
            },
            fingerprint=fp,
            advisory_flags=["HIGH_CONSEQUENCE_DETECTED"],
            previous_event_id="evt-ctx-2",
        ),
        _event(
            session_id=sid, event_id="evt-ctx-3",
            event_type="CONTEXT_SNAPSHOT", sequence=7, primitive="CONTEXT",
            payload={
                "data_classifications": ["confidential"],
                "classification_source": "explicit",
                "provenance": [".env"],
                "retention_flags": [],
                "context_size_tokens": 600,
                "llm_prompt_tokens": 600,
                "llm_completion_tokens": 120,
                "llm_turn_id": "turn-3",
            },
            fingerprint=fp,
            advisory_flags=["SENSITIVITY_ESCALATION"],
            policy_violations=["POL-005"],
            previous_event_id="evt-scope-3",
        ),
    ]


# ---------------------------------------------------------------------------
# Per-case driver
# ---------------------------------------------------------------------------


def _builder_for(case: str):
    return {
        "clean": _build_clean_events,
        "missing_intent": _build_missing_intent_events,
        "mixed_violations": _build_mixed_violations_events,
    }[case]


def _write_jsonl(path: Path, events: List[Dict[str, Any]]) -> None:
    """Canonical NDJSON, fixed insertion order — matches v0.2.5 demo."""
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )


def _generate_case(case: str) -> Dict[str, Any]:
    """Build, write, and pulse one showcase sub-case.

    Returns a small summary dict the demo's main() prints to the
    operator.
    """
    case_dir = SHOWCASE_DIR / case
    profile_path = case_dir / "profile.yaml"
    trace_path = case_dir / "session.jsonl"
    pulse_path = case_dir / "pulse_output.md"

    profile = GovernanceProfile.from_file(profile_path)
    events = _builder_for(case)(profile)
    _write_jsonl(trace_path, events)

    result = compute_pulse(events)
    # Attach the showcase-pinned sync_prompt eligibility BEFORE
    # rendering so the saved Markdown surface includes the sync
    # footer — that's what an operator would see when they actually
    # run `sentience pulse` for the first time.
    result["sync_prompt"] = dict(_SHOWCASE_SYNC_PROMPT)

    pulse_path.write_text(render_pulse_markdown(result), encoding="utf-8")

    return {
        "case": case,
        "profile_fingerprint": profile.fingerprint(),
        "session_id": result["session_id"],
        "status": result["status"],
        "trace": trace_path,
        "pulse": pulse_path,
    }


def _generate_v025_retrofit() -> Dict[str, Any]:
    """Render a pulse_output.md for the v0.2.5 closed-loop showcase.

    The v0.2.5 trace is clean in v0.2.6 terms (zero policy_violations,
    profile loaded), so its pulse is the canonical clean-session
    cross-link from the older showcase. Reads the existing pinned
    trace; does not regenerate it.
    """
    v025_trace = V025_SHOWCASE_DIR / "session.jsonl"
    v025_pulse_out = V025_SHOWCASE_DIR / "pulse_output.md"
    events = [
        json.loads(line)
        for line in v025_trace.read_text().splitlines()
        if line.strip()
    ]
    result = compute_pulse(events)
    result["sync_prompt"] = dict(_SHOWCASE_SYNC_PROMPT)
    v025_pulse_out.write_text(
        render_pulse_markdown(result), encoding="utf-8"
    )
    return {
        "case": "v025-retrofit",
        "session_id": result["session_id"],
        "status": result["status"],
        "trace": v025_trace,
        "pulse": v025_pulse_out,
    }


# ---------------------------------------------------------------------------
# Operator-facing entry point
# ---------------------------------------------------------------------------


def main() -> None:
    print("=" * 70)
    print("v0.2.6 — Sentience Pulse Demo (three operator stories)")
    print("=" * 70)
    print()

    summaries = [_generate_case(case) for case in CASES]
    summaries.append(_generate_v025_retrofit())

    for s in summaries:
        print(f"[{s['case']}]")
        print(f"  session_id     : {s['session_id']}")
        print(f"  pulse status   : {s['status']}")
        print(f"  trace          : {s['trace']}")
        print(f"  pulse output   : {s['pulse']}")
        print()

    print("─" * 70)
    print("Run the live pulse CLI against any of these traces:")
    print()
    for case in CASES:
        print(
            f"    sentience pulse {SHOWCASE_DIR / case / 'session.jsonl'}"
        )
    print()


if __name__ == "__main__":
    main()
