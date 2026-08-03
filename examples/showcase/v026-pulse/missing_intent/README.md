# Missing-intent session

**What this case shows.** A Claude Code-style trace where intent is
never declared. The profile asks for `INTENT_DECLARED` before the
first mutating SCOPE_ASSERTED — but the runtime surface doesn't
expose an intent-declaration primitive today, so the trace lacks
that event entirely. Every mutating SCOPE_ASSERTED fires POL-001.

**When you'd see this.** Real Claude Code sessions today. The
Claude Code hook captures mutating tool calls but has no primitive
for the agent to declare why it's doing them. This is a known
v0.2.6.1 follow-up scope (the F-V5 SessionEnd token-capture spike
deferred this work).

**What pulse tells you.** Three things:

1. 100% of session compute attributes to undeclared turns. Pulse
   names this honestly — the Undeclared-intent section's "Why it
   matters" line reads *"every attributed turn is classified as
   undeclared. Often a surface-bound limitation (e.g. Claude Code
   today), not agent drift."*
2. POL-001 fired on every mutating turn (3 turns, 3,960 tokens).
3. The next step isn't to "fix the agent" — it's to wait for the
   v0.2.6.1 runtime work or wire intent declaration explicitly into
   the workflow.

This is the case where pulse's framing discipline matters most:
naming the limitation distinguishes surface-bound from agent-bound
in the first 60 seconds of reading the report.

## Files

| File | What it is |
|---|---|
| `profile.yaml` | Same shape as the clean case — intent required at first write. The profile is fine; the trace is the diff. |
| `session.jsonl` | Synthesized trace; 7 events, 3 reasoning turns, no INTENT_DECLARED. |
| `pulse_output.md` | Pulse Markdown report — pre-rendered, byte-stable. |

Regenerate via `python examples/v026_pulse_demo.py`. Re-run the
live CLI: `sentience pulse examples/showcase/v026-pulse/missing_intent/session.jsonl`.
