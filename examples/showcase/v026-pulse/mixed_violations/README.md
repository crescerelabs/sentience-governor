# Mixed-violations session

**What this case shows.** A session under a tighter profile where
multiple distinct POL rules fire across multiple turns. POL-001
(intent missing) fires on Turn 1's mutating write. POL-003
(`CONTEXT_UNCLASSIFIED`) fires on Turn 2's snapshot of an
unclassified CSV read. POL-005 (sensitivity escalation) fires on
Turn 3's snapshot of a `.env` write that escalates classification
from internal → confidential.

**When you'd see this.** A tightening profile catching real drift.
A multi-rule pulse output is the case where the per-rule
prioritization signal becomes visible — the rule with the most
associated compute is the first one to inspect.

**What pulse tells you.** Four things:

1. Per-rule breakdown ranks POL-003 first by associated compute
   (2,400 tokens across 1 turn). The "Why it matters" line points
   the operator there as the first rule to inspect.
2. The non-additivity callout fires inline between the by-rule
   rows and the why-it-matters line — operators see at the moment
   they read the per-rule table that summing tokens across rules
   can exceed total compute when a single turn fires multiple
   rules.
3. Three distinct advisory flags fired: `CONTEXT_UNCLASSIFIED`,
   `HIGH_CONSEQUENCE_DETECTED` (the `.env` write matched the
   profile's high-consequence pattern), `SENSITIVITY_ESCALATION`.
4. The Undeclared-intent section surfaces the 1,680 tokens that
   attached to the unattributed first turn — distinct signal from
   the per-rule view.

This case demonstrates the v0.2.6 acceptance criterion that pulse
helps an operator understand an agent session faster than they
otherwise could have.

## Files

| File | What it is |
|---|---|
| `profile.yaml` | Tighter profile — three task-boundary signals, three high-consequence patterns including the `.env` rule that fires on Turn 3. |
| `session.jsonl` | Synthesized trace; 7 events, 3 reasoning turns, three distinct POL firings. |
| `pulse_output.md` | Pulse Markdown report — pre-rendered, byte-stable. |

Regenerate via `python examples/v026_pulse_demo.py`. Re-run the
live CLI: `sentience pulse examples/showcase/v026-pulse/mixed_violations/session.jsonl`.
