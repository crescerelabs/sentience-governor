"""v0.2.5 closed-loop demo — runnable end-to-end.

Demonstrates the full governance loop introduced in v0.2.5:

  1. Operator authors a governance profile (see
     ``examples/showcase/v025-closed-loop/profile.yaml``).
  2. A governed session runs; the wrapper emits a trace that carries
     the profile fingerprint on every event and fires the v0.2.5
     advisory flags (``TASK_BOUNDARY_CROSSED``,
     ``HIGH_CONSEQUENCE_DETECTED``) when the agent's behavior
     matches the profile's signals.
  3. The analyzer reads the trace and surfaces the new flags in
     dedicated sections of the report (CLI + Markdown).
  4. The operator reviews the analyzer output and tunes the profile.

This script does steps 1-3 in-process: it loads the example profile,
synthesizes a representative trace (no live agent needed), runs the
analyzer, and writes:

  * ``session.jsonl``       — the synthesized trace
  * ``analyzer_output.md``  — the rendered Markdown report

Both outputs are byte-stable across runs (the trace uses fixed
event_ids + fixed timestamps; the analyzer + renderers are pure
functions). Tests pin this to catch unintended drift.

Run::

    python examples/v025_closed_loop_demo.py

Re-run via the CLI to see the same output through the live
analyzer::

    sentience analyze undeclared-intent examples/showcase/v025-closed-loop/session.jsonl
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from sentience_governor.analyze import (
    compute_undeclared_intent_spend,
    render_cli,
    render_markdown_report,
)
from sentience_governor.profile import GovernanceProfile

SHOWCASE_DIR = Path(__file__).parent / "showcase" / "v025-closed-loop"
PROFILE_PATH = SHOWCASE_DIR / "profile.yaml"
TRACE_PATH = SHOWCASE_DIR / "session.jsonl"
REPORT_PATH = SHOWCASE_DIR / "analyzer_output.md"

# Fixed identifiers — bytes-stable across runs.
SESSION_ID = "demo-v025-closed-loop-0001"
AGENT_ID = "scoped-coder-agent-v1"
TIMESTAMP = "2026-05-12T10:00:00.000Z"


def _event(
    *,
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
    """Build one governance event with all v0.2.5 envelope fields.

    Mirrors the shape ``GovernanceEvent.to_dict()`` produces under a
    profile-loaded session (fingerprint populated, None-omitted
    fields absent).
    """
    return {
        "event_id": event_id,
        "event_type": event_type,
        "session_id": SESSION_ID,
        "event_sequence_number": sequence,
        "previous_event_id": previous_event_id,
        "agent_id": AGENT_ID,
        "deployment_mode": "vendor_managed",
        "timestamp_utc": TIMESTAMP,
        "primitive": primitive,
        "payload": payload,
        "advisory_flags": list(advisory_flags or []),
        "policy_violations": list(policy_violations or []),
        "simulated_consequence": None,
        "pass_through": True,
        "profile_fingerprint": fingerprint,
    }


def build_session_events(profile: GovernanceProfile) -> List[Dict[str, Any]]:
    """Return the synthesized closed-loop trace.

    Story:
      * The agent declares its intent: "refactor src/auth/ middleware
        to add retry-on-401 logic."
      * Turn 1 is in-scope (reads the existing module).
      * Turn 2 crosses a task boundary (read → write transition).
      * Turn 3 fires high-consequence detection (Bash rm -rf).
    """
    fp = profile.fingerprint()

    return [
        _event(
            event_id="evt-reg",
            event_type="AGENT_REGISTERED",
            sequence=1,
            primitive="REGISTRATION",
            payload={
                "agent_id": AGENT_ID,
                "agent_version": "0.1.0",
                "vendor_id": "example-co",
                "deployment_mode": "vendor_managed",
                "declared_capabilities": ["fs.read", "fs.write", "Bash"],
                "owner_claim": "operator@example.com",
                "policy_context": None,
                # v0.2.5 — profile metadata pinned on AGENT_REGISTERED.
                "profile_loaded": True,
                "profile_schema_version": profile.schema_version,
            },
            fingerprint=fp,
        ),
        _event(
            event_id="evt-intent",
            event_type="INTENT_DECLARED",
            sequence=2,
            primitive="INTENT",
            payload={
                "stated_objective": (
                    "refactor src/auth/ middleware to add retry-on-401 logic"
                ),
                "intent_source": "explicit",
                "intent_confidence": "explicit",
                "authorization_claim": "operator@example.com",
                "session_scope_hint": ["fs.read", "fs.write"],
            },
            fingerprint=fp,
            previous_event_id="evt-reg",
        ),
        # Turn 1 — declared READ.
        _event(
            event_id="evt-scope-1",
            event_type="SCOPE_ASSERTED",
            sequence=3,
            primitive="SCOPE",
            payload={
                "tool_id": "fs.read",
                "asserted_permissions": ["read"],
                "target_system": "src/auth/middleware.py",
                "operation_type": "READ",
            },
            fingerprint=fp,
            previous_event_id="evt-intent",
        ),
        _event(
            event_id="evt-ctx-1",
            event_type="CONTEXT_SNAPSHOT",
            sequence=4,
            primitive="CONTEXT",
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
            fingerprint=fp,
            previous_event_id="evt-scope-1",
        ),
        # Turn 2 — WRITE. Crosses task boundary (read → write transition).
        _event(
            event_id="evt-scope-2",
            event_type="SCOPE_ASSERTED",
            sequence=5,
            primitive="SCOPE",
            payload={
                "tool_id": "fs.write",
                "asserted_permissions": ["write"],
                "target_system": "src/auth/middleware.py",
                "operation_type": "WRITE",
            },
            fingerprint=fp,
            advisory_flags=["TASK_BOUNDARY_CROSSED"],
            previous_event_id="evt-ctx-1",
        ),
        _event(
            event_id="evt-ctx-2",
            event_type="CONTEXT_SNAPSHOT",
            sequence=6,
            primitive="CONTEXT",
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
            fingerprint=fp,
            previous_event_id="evt-scope-2",
        ),
        # Turn 3 — Bash rm -rf — fires HIGH_CONSEQUENCE_DETECTED.
        _event(
            event_id="evt-scope-3",
            event_type="SCOPE_ASSERTED",
            sequence=7,
            primitive="SCOPE",
            payload={
                "tool_id": "Bash",
                "asserted_permissions": ["execute"],
                "target_system": "rm -rf /tmp/scratch-build-dir",
                "operation_type": "EXECUTE",
            },
            fingerprint=fp,
            advisory_flags=["HIGH_CONSEQUENCE_DETECTED"],
            previous_event_id="evt-ctx-2",
        ),
        _event(
            event_id="evt-ctx-3",
            event_type="CONTEXT_SNAPSHOT",
            sequence=8,
            primitive="CONTEXT",
            payload={
                "data_classifications": ["internal"],
                "classification_source": "explicit",
                "provenance": [],
                "retention_flags": [],
                "context_size_tokens": 800,
                "llm_prompt_tokens": 800,
                "llm_completion_tokens": 160,
                "llm_turn_id": "turn-3",
            },
            fingerprint=fp,
            previous_event_id="evt-scope-3",
        ),
    ]


def _write_jsonl(path: Path, events: List[Dict[str, Any]]) -> None:
    """Write events as canonical NDJSON.

    Each event is dumped with ``sort_keys=False`` to preserve the
    insertion order (which mirrors the wire format). We use the
    ``json`` defaults — no float formatting concerns because the
    trace has no floats.
    """
    path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    profile = GovernanceProfile.from_file(PROFILE_PATH)
    events = build_session_events(profile)

    _write_jsonl(TRACE_PATH, events)
    result = compute_undeclared_intent_spend(events)
    REPORT_PATH.write_text(render_markdown_report(result), encoding="utf-8")

    print("=" * 70)
    print("v0.2.5 — Closed-Loop Governance Demo")
    print("=" * 70)
    print()
    print(f"Profile        : {PROFILE_PATH}")
    print(f"Fingerprint    : {profile.fingerprint()}")
    print(f"Trace          : {TRACE_PATH}")
    print(f"Report         : {REPORT_PATH}")
    print(f"Session id     : {SESSION_ID}")
    print(f"Status         : {result['status']}")
    print()
    print("─" * 70)
    print("CLI render")
    print("─" * 70)
    print(render_cli(result))
    print()
    print("Re-analyze via the CLI:")
    print(f"    sentience analyze undeclared-intent {TRACE_PATH}")


if __name__ == "__main__":
    main()
