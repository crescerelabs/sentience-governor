# Clean session

**What this case shows.** A well-behaved agent under a permissive
governance profile. The agent declares its operational intent up
front, performs a read + a write on the same file, and produces no
policy violations and no advisory flags.

**When you'd see this.** A working profile + a session that stayed
within the bounds you authored. Per the v0.2.5 plan §F9 finding,
this is the most-common first-session outcome for fresh operators
who just ran `sentience profile init` — the default profile is
permissive enough that typical Claude Code sessions don't fire
violations.

**What pulse tells you.** Three things:

1. Per-turn token attribution worked end-to-end — every reasoning
   turn carries `llm_turn_id` and token totals.
2. The profile was loaded and active (fingerprint pinned on every
   event in the trace).
3. No policy rules fired against the rules active in this session.

The Interpretation block makes the recurring-value point explicit:
*the run-to-run evidence record is intact*. Pulse will surface more
signal as your profile tightens or agent behavior shifts.

## Files

| File | What it is |
|---|---|
| `profile.yaml` | Permissive governance profile (intent required at first write, one task-boundary signal, one high-consequence pattern). |
| `session.jsonl` | Synthesized trace; 6 events, 2 reasoning turns, fixed timestamps. |
| `pulse_output.md` | Pulse Markdown report — pre-rendered, byte-stable. |

Regenerate via `python examples/v026_pulse_demo.py`. Re-run the
live CLI: `sentience pulse examples/showcase/v026-pulse/clean/session.jsonl`.
