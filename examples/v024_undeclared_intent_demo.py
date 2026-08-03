"""v0.2.4 undeclared-intent analyzer demo — runnable end-to-end.

Builds a small synthesized session in memory, runs the v0.2.4
analyzer over it, and prints both the human-readable CLI render and
the JSON output. No MCP wrapper, no LangChain — this demo is about
the analyzer surface alone.

The trace shape models a realistic operator scenario:

  * The agent declares a narrow operational intent ("export Q3
    revenue summary as PDF") via an INTENT_DECLARED event.
  * It executes four reasoning turns. Three are in-scope of the
    declared intent (read CRM, format report, write file). One
    drifts off-task — the agent calls a Slack write API that has
    nothing to do with the declared objective. The wrapper marks
    that turn's SCOPE_ASSERTED with policy_violations=["POL-001"]
    (write without declared intent).
  * Each turn carries a CONTEXT_SNAPSHOT with populated tokens.

The analyzer attributes the off-task turn's tokens to undeclared
spend, marks the session_has_declared_intent flag true, and surfaces
the whole result through the locked v0.2.4 schema.

Run::

    python examples/v024_undeclared_intent_demo.py

The script writes the fixture trace to /tmp/v024-undeclared-intent-
demo.jsonl so you can re-analyze it via the CLI::

    sentience analyze undeclared-intent /tmp/v024-undeclared-intent-demo.jsonl

For the three pre-rendered Markdown showcase reports (low / high /
surface-bound), see ``examples/showcase/``.
"""

from __future__ import annotations

import json
from pathlib import Path

from sentience_governor.analyze import (
    compute_undeclared_intent_spend,
    render_cli,
    render_markdown_report,
)

# Single source of truth: the session builder now lives inside the
# package (sentience_governor/demos/) so `sentience demo undeclared-intent`
# and this example share one definition. See F-V6 (v0.2.5.2).
from sentience_governor.demos.undeclared_intent import (
    SESSION_ID,
    build_session_events,
)


def main() -> None:
    events = build_session_events()

    # Persist the fixture so the user can re-analyze via the CLI.
    out_path = Path("/tmp/v024-undeclared-intent-demo.jsonl")
    out_path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    result = compute_undeclared_intent_spend(events)

    print("=" * 70)
    print("v0.2.4 — Undeclared-Intent Token Spend (analyzer demo)")
    print("=" * 70)
    print()
    print(f"Trace written to: {out_path}")
    print(f"Session id      : {SESSION_ID}")
    print(f"Status          : {result['status']}")
    print()
    print("─" * 70)
    print("CLI render")
    print("─" * 70)
    print(render_cli(result))
    print("─" * 70)
    print("Markdown report (excerpt)")
    print("─" * 70)
    md = render_markdown_report(result)
    # Print first 25 lines so the demo stays one-screen-friendly.
    print("\n".join(md.splitlines()[:25]))
    print("...")
    print()
    print("─" * 70)
    print("JSON output (analyzer schema)")
    print("─" * 70)
    print(json.dumps(result, indent=2))
    print()
    print("Re-run via the CLI:")
    print(f"    sentience analyze undeclared-intent {out_path}")


if __name__ == "__main__":
    main()
