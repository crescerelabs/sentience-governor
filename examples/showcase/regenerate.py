"""Regenerate the v0.2.4 undeclared-intent showcase reports.

Three deliberate scenarios, each rendered to a Markdown file in this
directory:

  * sample_report_low_undeclared.md   — agent mostly on-task
                                        (~10% undeclared spend)
  * sample_report_high_undeclared.md  — agent drifts heavily
                                        (~50% undeclared spend)
  * sample_report_no_intent.md        — surface lacks intent
                                        primitive (Claude Code today);
                                        every turn is undeclared

The fixtures are encoded inline so this file is the single source of
truth. Re-running this script must produce byte-identical Markdown
output (the analyzer + renderers are pure functions, fixtures are
deterministic, and the rendered Markdown contains no timestamps or
random IDs).

Run::

    python examples/showcase/regenerate.py

Verify byte-stability::

    python examples/showcase/regenerate.py
    git diff --exit-code examples/showcase/sample_report_*.md
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from sentience_governor.analyze import (
    compute_undeclared_intent_spend,
    render_markdown_report,
)


SHOWCASE_DIR = Path(__file__).parent


def _intent(session_id: str, objective: str) -> Dict[str, Any]:
    return {
        "event_type": "INTENT_DECLARED",
        "session_id": session_id,
        "advisory_flags": [],
        "policy_violations": [],
        "payload": {"stated_objective": objective},
    }


def _scope(
    session_id: str,
    tool_id: str,
    *,
    advisory: List[str] | None = None,
    policy: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "event_type": "SCOPE_ASSERTED",
        "session_id": session_id,
        "advisory_flags": list(advisory or []),
        "policy_violations": list(policy or []),
        "payload": {"tool_id": tool_id},
    }


def _ctx(
    session_id: str,
    turn_id: str,
    *,
    prompt: int,
    completion: int,
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


# ---------------------------------------------------------------------------
# Scenario 1 — low undeclared spend.
#
# Agent declared "summarize Q3 sales pipeline" and stayed on-task for
# nine of ten turns. One turn drifted (a quick Slack ping unrelated to
# the report).
# ---------------------------------------------------------------------------


def low_undeclared_session() -> List[Dict[str, Any]]:
    sid = "low-undeclared-2026-q3-pipeline"
    events: List[Dict[str, Any]] = [
        _intent(sid, "summarize Q3 sales pipeline"),
    ]
    declared_tools = [
        "crm.list_opportunities",
        "crm.list_accounts",
        "analytics.aggregate",
        "analytics.aggregate",
        "template.render",
        "template.render",
        "fs.read",
        "fs.write",
        "fs.write",
    ]
    for i, tool in enumerate(declared_tools, start=1):
        events.append(_scope(sid, tool))
        events.append(_ctx(sid, f"turn-{i}", prompt=900, completion=180))
    # One off-task turn — drift.
    events.append(
        _scope(
            sid,
            "slack.write_message",
            advisory=["INTENT_MISSING"],
            policy=["POL-001"],
        )
    )
    events.append(_ctx(sid, "turn-10", prompt=900, completion=180))
    return events


# ---------------------------------------------------------------------------
# Scenario 2 — high undeclared spend.
#
# Agent declared "export Q3 revenue summary as PDF" but drifted into
# unrelated CRM writes and Postgres queries. Half the compute went to
# off-task work.
# ---------------------------------------------------------------------------


def high_undeclared_session() -> List[Dict[str, Any]]:
    sid = "high-undeclared-2026-q3-export"
    events: List[Dict[str, Any]] = [
        _intent(sid, "export Q3 revenue summary as PDF"),
    ]
    # Three on-task turns.
    on_task = [
        ("crm.list_invoices", 1200, 240),
        ("template.render", 900, 200),
        ("fs.write", 1100, 220),
    ]
    for i, (tool, p, c) in enumerate(on_task, start=1):
        events.append(_scope(sid, tool))
        events.append(_ctx(sid, f"turn-{i}", prompt=p, completion=c))
    # Three off-task turns — drift into unrelated systems.
    off_task = [
        ("slack.write_message", 950, 180),
        ("postgres.execute", 1400, 260),
        ("crm.update_contact", 1050, 200),
    ]
    for i, (tool, p, c) in enumerate(off_task, start=4):
        events.append(
            _scope(
                sid,
                tool,
                advisory=["INTENT_MISSING"],
                policy=["POL-001"],
            )
        )
        events.append(_ctx(sid, f"turn-{i}", prompt=p, completion=c))
    return events


# ---------------------------------------------------------------------------
# Scenario 3 — surface lacks an intent-declaration primitive.
#
# A Claude Code-style hook capture: token data is wired (rare today;
# this is what the future hook surface will produce) but no
# INTENT_DECLARED event was ever emitted because the surface does not
# yet expose an intent-declaration primitive. Every turn is classified
# as undeclared. The CLI / saved report use the surface-bound footer
# copy, NOT the agent-bound copy — the operator should not be told the
# agent drifted when the limitation is the surface itself.
# ---------------------------------------------------------------------------


def no_intent_session() -> List[Dict[str, Any]]:
    sid = "no-intent-claude-code-2026-04-17"
    events: List[Dict[str, Any]] = []
    tool_sequence = [
        ("Bash", 1500, 280),
        ("Read", 800, 140),
        ("Edit", 1200, 220),
        ("Bash", 1100, 200),
        ("Read", 700, 130),
        ("Write", 1300, 240),
    ]
    for i, (tool, p, c) in enumerate(tool_sequence, start=1):
        events.append(
            _scope(
                sid,
                tool,
                advisory=["INTENT_MISSING"],
                policy=["POL-001"],
            )
        )
        events.append(_ctx(sid, f"turn-{i}", prompt=p, completion=c))
    return events


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


SCENARIOS = [
    ("sample_report_low_undeclared.md", low_undeclared_session),
    ("sample_report_high_undeclared.md", high_undeclared_session),
    ("sample_report_no_intent.md", no_intent_session),
]


def main() -> None:
    SHOWCASE_DIR.mkdir(parents=True, exist_ok=True)
    for filename, factory in SCENARIOS:
        events = factory()
        result = compute_undeclared_intent_spend(events)
        body = render_markdown_report(result)
        out = SHOWCASE_DIR / filename
        out.write_text(body, encoding="utf-8")
        print(
            f"  {filename:<40}  status={result['status']:<8}  "
            f"undeclared={result['undeclared_percent']}%"
        )
    print()
    print(f"Wrote {len(SCENARIOS)} reports to {SHOWCASE_DIR}")


if __name__ == "__main__":
    main()
