# v0.2.6 — Sentience Pulse Showcase

`sentience pulse` is the v0.2.6 adoption surface — one command that
composes the undeclared-intent analyzer (v0.2.4), the policy-
violation burn-rate analyzer (v0.2.6), and the advisory-flag
summary into a single shareable report. This showcase walks
through the three states an operator actually encounters in
practice.

Each sub-case ships a profile, a synthesized governed session, and
the pulse output that pairs with it. Outputs are byte-stable; the
generator is `examples/v026_pulse_demo.py` and the byte-stability
guarantee is pinned by `tests/test_v026_pulse_demo.py`.

## The three stories

| Case | What it shows | When you'd see this |
|---|---|---|
| [`clean/`](./clean/) | A well-behaved agent under a permissive profile. No policy violations, no advisory flags, full per-turn attribution. | A working profile + a session that stayed within the bounds you authored. The most-common first-session outcome for fresh operators. |
| [`missing_intent/`](./missing_intent/) | Claude Code-style trace where the runtime doesn't expose an intent primitive. Every mutating turn surfaces as undeclared and fires POL-001. | Real Claude Code today. Pulse explains *why* the undeclared metric is high — surface-bound, not agent drift. |
| [`mixed_violations/`](./mixed_violations/) | A session under a tighter profile with several distinct POL rules firing across multiple turns. | When your profile is tight enough to catch real drift. Pulse's per-rule prioritization signal becomes visible — the rule with the most associated compute is the first one to inspect. |

## Running the demo

```bash
python examples/v026_pulse_demo.py
```

The demo regenerates every `session.jsonl` and `pulse_output.md`
under this directory plus a `pulse_output.md` cross-link for the
v0.2.5 closed-loop showcase. All outputs are byte-stable across
runs.

To see the same pulse output through the live CLI for any case:

```bash
sentience pulse examples/showcase/v026-pulse/clean/session.jsonl
sentience pulse examples/showcase/v026-pulse/missing_intent/session.jsonl
sentience pulse examples/showcase/v026-pulse/mixed_violations/session.jsonl
```

## Cross-link to v0.2.5

The v0.2.5 closed-loop showcase (`examples/showcase/v025-closed-loop/`)
is a clean session in v0.2.6 terms — the agent declared intent and no
policy rules fired. The retrofitted `pulse_output.md` there shows
what pulse looks like for that session; it complements the
`clean/` sub-case here.

## What pulse is NOT

* Not enforcement. Same observational posture as v0.2.0–v0.2.5. Pulse
  reports drift; it does not block or modify agent behavior.
* Not a savings estimate. Burn-rate copy uses association language
  only ("appeared on turns representing N tokens"), never causality
  or savings wording.
* Not a dashboard. Pulse is per-session, single-screen consumption.
  Cross-session aggregation is v0.3.x console territory.

## Saving and sharing pulse output

The Markdown report is designed to be standalone-understandable. Save
it via `sentience pulse --save` or paste it into a GitHub issue, a
Slack message, an advisor update, or a customer / investor proof
point. The "Why it matters" line in each section is what makes the
report readable cold, without context from the operator who ran the
session.
