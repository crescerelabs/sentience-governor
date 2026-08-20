# Sentience Governor — Architecture

> Long-form architecture reference for `sentience-governor` v0.2.5.1.
> For the user-facing quickstart and CLI reference, see [docs/guide/sentience_governor.md](./guide/sentience_governor.md).
> For release history, see [CHANGELOG.md](../CHANGELOG.md).

---

## Why this exists

Most AI governance today is dashboard-shaped, vendor-locked, and after-the-fact. It captures what happened on someone else's server, in someone else's format, for someone else's policy. By the time a compliance team sees a trace, the agent is three sessions past the divergence.

Sentience Governor is the opposite shape. It runs locally on the operator's machine. It sits beside the agent runtime, not in front of it. It produces a deterministic event stream that compares what the agent actually did to what the operator declared. The wrapper is small; the schema is fixed; the analyzer reads local logs and never re-infers.

This document explains how that works at the mechanism level.

---

## Table of Contents

- [Architecture at a glance](#architecture-at-a-glance)
- [Four design properties](#four-design-properties)
- [The Governance Profile](#the-governance-profile)
- [The Event Pipeline](#the-event-pipeline)
- [Advisory Flags](#advisory-flags)
- [Profile Fingerprint](#profile-fingerprint)
- [Three Runtime Surfaces](#three-runtime-surfaces)
- [The Analyzer](#the-analyzer)
- [Open Tier vs Paid Tier](#open-tier-vs-paid-tier)
- [Comparison with Other Tools](#comparison-with-other-tools)
- [What v0.2.5 is not](#what-v025-is-not)
- [Further reading](#further-reading)

---

## Architecture at a glance

Every agent action flows through one deterministic pipeline:

```
   ┌──────────────┐     ┌──────────────┐     ┌──────────────────┐     ┌─────────────┐     ┌──────────────┐
   │              │     │              │     │                  │     │             │     │              │
   │ Agent action ├────►│   Runtime    ├────►│      Policy      ├────►│ Local event ├────►│   Analyzer   │
   │              │     │   wrapper    │     │   evaluation     │     │     log     │     │    (CLI)     │
   │              │     │              │     │                  │     │             │     │              │
   └──────────────┘     └──────┬───────┘     └──────────────────┘     └─────────────┘     └──────────────┘
                               │
                        Wrapper sits BESIDE
                        the runtime, never inline.
                        Observed at the execution
                        boundary, not intercepted.
```

The five stages:

1. **Agent action.** The agent calls a tool, writes a file, executes a command. Sentience does not interfere with the call.
2. **Runtime wrapper.** A thin observer attached to the runtime (Claude Code hook, MCP wrapper, or LangChain handler) captures the action's structured metadata. No prompts inspected. No completions inspected.
3. **Policy evaluation.** The EventBuilder evaluates the action against the default policy set plus the operator's profile (if one exists at `~/.sentience/profile.yaml`). Pure deterministic logic over structured metadata.
4. **Local event log.** A single governance event is appended to a local newline-delimited JSON file. Same event schema across every runtime surface and every operator's machine.
5. **Analyzer (CLI).** `sentience analyze` reads the local log and surfaces drift, task boundaries crossed, high-consequence operations, and undeclared intent. Pure-function, replay-stable.

By default, nothing leaves the machine. The two network-capable paths (an
operator-configured sink, and the one-time launch-list prompt) are both opt-in
and neither is on the governance path. See the README for the detail.

---

## Four design properties

These four properties are the spine of every architectural decision in v0.2.x.

| Property | What it means in v0.2.5 |
|---|---|
| **Wrapper, not gateway** | The wrapper sits beside the agent runtime. It does not proxy the call. There is no Sentience-shaped server the agent has to route through. Zero added latency to the agent's request path. The agent does not break if Sentience is removed. |
| **Payload-free** | Sentience never inspects prompts, completions, or any other model-content payload. Only structured metadata (tool_id, target_system, working directory, file extension, time delta, etc.) crosses into the EventBuilder. This is the load-bearing compliance posture: the operator does not need to negotiate with a governance vendor about what the vendor sees. |
| **Deterministic** | Policy evaluation is pure logic computed from structured metadata. No LLM judge. No learned models. No probabilistic inference. The same input metadata always produces the same event with the same flags, the same policy_violations, and the same simulated_consequence. |
| **Local-first** | The profile lives at `~/.sentience/profile.yaml`. The event log lives on the operator's machine. The analyzer is a CLI tool. No account. No cloud. No telemetry. Governance runs with the network off; the two network-capable paths (an operator-configured sink, and the one-time launch-list prompt) are opt-in and neither is on the governance path. |

These four properties define what stays the same across every Sentience release. The paid tier adds central event ingestion, an agent registry, an enforcement engine, organizational memory, and compliance surfaces. The four properties of the wrapper itself do not change.

---

## The Governance Profile

The governance profile is the first operator-authored artifact in Sentience. It is a single YAML file at `~/.sentience/profile.yaml`. The operator writes it. The wrapper reads it on every governed session. The runtime evaluates against it deterministically.

Schema version 1 has three sections, each addressing a real operator pain:

```yaml
schema_version: 1

session_intent:
  demand_at: first_write

task_boundary:
  signals:
    - dir_change
    - file_type_shift
    - read_to_write_transition

high_consequence:
  tools:
    - "Bash:.*rm.*-rf.*"
    - "fs.write:.*\\.env.*"
```

### `session_intent.demand_at`

Controls when the wrapper asks "what is this session for?". Three values:

- `session_start` — wrapper fires the intent-declaration check at session start before any tool call.
- `first_write` (default) — wrapper stays silent until the agent attempts a mutating operation, then fires the check.
- `never` — wrapper does not fire the intent-declaration check.

The check itself is observational in the open tier. When intent has not been declared and a mutating operation is attempted, the wrapper attaches `INTENT_MISSING` to the next `SCOPE_ASSERTED` event. The operator sees it in the local log; the agent is not blocked.

### `task_boundary.signals`

Detects when the agent has crossed into a new task. Four signals available:

- `dir_change` — the working directory changed between events.
- `file_type_shift` — the file extension of the active file changed (e.g. `.py` to `.tsx`).
- `time_gap` — significant elapsed time between events.
- `read_to_write_transition` — agent moved from a read-only phase to mutating operations.

The operator picks which signals are meaningful. Each signal fires by comparing the current event's normalized metadata against the prior event in the session. When any signal fires, the wrapper attaches `TASK_BOUNDARY_CROSSED` to the next `SCOPE_ASSERTED` event.

### `high_consequence.tools`

A list of regex patterns matched against `<tool_id>:<target_system>`. Matches attach `HIGH_CONSEQUENCE_DETECTED` to that event.

Regex compilation is cached per session. Matching is O(n) over the operator's pattern list, evaluated once per `TOOL_CALL_ATTEMPTED`. The operator authors the patterns; Sentience does not ship opinions about what counts as high-consequence (with the exception of the default policy set, see below).

### Reserved sections

Three sections are reserved in the schema but not implemented in v0.2.5:

- `extends` — profile inheritance / composition.
- `policies` — operator-level policy customization on top of the default rule set.
- `custom_rules` — operator-defined policy rules.

These slots exist so future operator-authored governance composes additively rather than requiring schema migration. Future-tier `on_match` vocabulary (`block`, `prompt`, `deny`) is also reserved at the schema level; in v0.2.5 every `on_match` value warns at load time and falls back to `flag`.

### Validation discipline

The profile loader is read-only. `sentience profile validate` checks the schema, reports per-field errors and warnings, and never mutates the operator-authored file. If the profile is malformed, the wrapper proceeds without it and the session continues to be governed by the default policy set.

---

## The Event Pipeline

The wrapper emits six event types. The set is fixed in v0.2.x.

| Event | When it fires | Carries |
|---|---|---|
| `SESSION_START` | First wrapper invocation in a session | Session ID, profile_fingerprint (if profile present), schema_version |
| `INTENT_DECLARED` | Operator (or agent on operator's behalf) declares session intent | Intent text, declaration source |
| `SCOPE_ASSERTED` | After every `TOOL_CALL_ATTEMPTED`, summarizes the scope of the action | advisory_flags, policy_violations, simulated_consequence |
| `TOOL_CALL_ATTEMPTED` | Agent attempts a tool call | tool_id, target_system, structured metadata only |
| `MEMORY_WRITE_ATTEMPTED` | Agent attempts to persist information across sessions | write target, structured metadata only |
| `SESSION_END` | Wrapper teardown | Session duration, event count, terminal state |

Every event carries `profile_fingerprint` at the envelope level when a profile is active. Same fingerprint on every event in the same session. Useful for one session; load-bearing for the cross-session memory layer planned for v0.3.x.

The pipeline is single-threaded per session. There is no event queue, no batching, no out-of-band processing. The EventBuilder evaluates synchronously and the sink writer appends to the local log before the wrapper returns control to the runtime. Latency overhead is bounded by the regex match count in `high_consequence.tools` plus the cost of one local-file append.

---

## Advisory Flags

Advisory flags are how the EventBuilder communicates evaluation results to the analyzer. They are attached to event envelopes, never to payloads. v0.2.5 ships ten advisory flag conditions:

| Flag | Attached to | Fires when |
|---|---|---|
| `INTENT_MISSING` | `SCOPE_ASSERTED` | `session_intent.demand_at` threshold was reached without a declared intent |
| `TASK_BOUNDARY_CROSSED` | `SCOPE_ASSERTED` | Any signal in `task_boundary.signals` fired |
| `HIGH_CONSEQUENCE_DETECTED` | `TOOL_CALL_ATTEMPTED` | `<tool_id>:<target_system>` matched a pattern in `high_consequence.tools` |
| `POLICY_DEFAULT_FIRED` | Any event | One of the five default policy rules (POL-001 through POL-005) matched |
| `MEMORY_WRITE_OUTSIDE_SCOPE` | `MEMORY_WRITE_ATTEMPTED` | Agent attempted to persist memory beyond the session's declared scope |
| `TOOL_RECENCY_VIOLATION` | `TOOL_CALL_ATTEMPTED` | Same destructive tool fired multiple times within a short window |
| `READ_TO_WRITE_TRANSITION` | `SCOPE_ASSERTED` | Detected as a sub-signal of task_boundary; surfaced standalone for analyzer use |
| `TARGET_SYSTEM_SHIFT` | `TOOL_CALL_ATTEMPTED` | Target system changed from prior tool call (e.g. local fs to remote API) |
| `CONTEXT_GROWTH_THRESHOLD` | `SCOPE_ASSERTED` | Cumulative context size exceeded a sanity threshold for the session shape |
| `SESSION_INTENT_DRIFT` | `SCOPE_ASSERTED` | Execution metadata diverged from declared intent vocabulary |

Flags are additive. Multiple flags can be attached to the same event. The analyzer's job is to surface clusters of flags as evidence; the operator's job is to decide what to do about them. In the open tier, no flag triggers an action against the agent.

`policy_violations` lists the specific rule IDs that matched. `simulated_consequence` is a short string describing what would have happened if enforcement were on (e.g. `"would have blocked"`, `"would have flagged"`, `"would have required confirmation"`). These two fields are reserved for the paid tier's enforcement engine and produce informational output in the open tier.

---

## Profile Fingerprint

`profile_fingerprint` is a 12-character SHA-256 truncation computed deterministically from the profile content. Three properties:

- **Whitespace-normalized.** Trailing newlines, YAML formatting differences, and comment additions do not change the fingerprint.
- **Key-ordered.** Sections and keys are sorted to a canonical order before hashing, so equivalent YAML produces equivalent fingerprints regardless of how the operator wrote them.
- **Content-only.** Comments are stripped before hashing; the fingerprint reflects only what the wrapper actually evaluates against.

The fingerprint is attached to every event in a governed session. Same profile content always produces the same fingerprint. Traces are correlatable back to the exact profile they were produced against, even across machines.

This is the carrier primitive for the cross-session memory tier planned for v0.3.x. Today the fingerprint enables single-session correlation; in v0.3.x it enables longitudinal drift detection (does the operator's profile change correlate with changes in advisory flag frequency over time?).

---

## Three Runtime Surfaces

The same profile, the same event schema, and the same advisory flag vocabulary apply across three runtime entry points.

### Claude Code hook

The hook runs on every Claude Code session start. It reads `~/.sentience/profile.yaml`, validates the schema, computes the fingerprint, and attaches the resolved `GovernanceProfile` object to the session context. Every subsequent `TOOL_CALL_ATTEMPTED` event the hook emits has profile-derived flags pre-attached by the EventBuilder before the sink writer sees it.

Installation: `sentience init claude-code` writes the hook configuration to `.claude/settings.json`. The hook is opt-in per-project; uninstalling removes the configuration cleanly.

### MCP wrapper

`sentience_governor.wrapper.mcp.wrap_mcp_client(client)` returns a wrapped MCP client. The wrapper resolves the profile on construction and attaches it to the client's middleware chain. Tool calls routed through the wrapped client run the same EventBuilder pipeline. The resolved profile travels with the client instance, not the call.

Compatible with any MCP-spec-compliant client. Wraps the call surface; does not modify the protocol.

### LangChain handler

Two integration points:

- `SentienceCallbackHandler` — attaches to the LangChain callback graph. Resolves the profile on instantiation. Hooks `on_tool_start` and `on_tool_end`.
- `SentienceMiddleware` — for newer LangChain agent patterns that use middleware composition rather than callbacks. Same behavior, different attachment surface.

Both work without modifying the agent's control flow. The handler observes; it does not gate.

### Resolution path consistency

All three surfaces resolve the profile the same way:

1. Read `~/.sentience/profile.yaml`.
2. Validate against `schema_version: 1`.
3. Compute the fingerprint.
4. Attach to the session context.

No environment variables. No constructor flags. The profile lives where the operator put it, and every wrapper surface knows where to look.

---

## The Analyzer

The analyzer is a deterministic, pure-function CLI tool that reads the local event log and produces a Markdown report.

```
$ sentience analyze undeclared-intent --latest
```

Output sections (when a profile is active):

- **Summary** — total tokens, undeclared-intent fraction, advisory flag counts
- **Profile** — fingerprint, schema_version, profile path
- **High-consequence operations** — table of matched tools per turn with pattern and target system
- **Task boundaries crossed** — table of boundary-crossing events with the specific signal that fired (dir_change / file_type_shift / time_gap / read_to_write_transition)
- **Undeclared turns** — per-turn breakdown of execution that touched systems outside declared intent

Replay-stability is a contract. Re-running the analyzer over the same trace produces byte-identical Markdown. This matters for CI integration, snapshot testing, and longitudinal comparison across sessions.

Three output flags: `--json` (structured), `--save` (no prompt; writes to `~/.sentience/reports/`), `--no-prompt` (headless mode for CI).

The analyzer reads only what the wrapper wrote. It does not call back to any LLM. It does not re-infer intent from completions. If the wrapper did not capture a field, the analyzer cannot surface it. This constraint is what makes the analyzer trustable.

---

## Open Tier vs Paid Tier

Both tiers run on the same event schema and the same control points. The wrapper code is identical. What changes is the deployment surface and the post-event handling.

| | Open Tier (today, Apache 2.0) | Paid Tier (Horizon 2) |
|---|---|---|
| **Scope** | Single operator, single machine | Cross-operator, organizational |
| **Profile authoring** | Operator writes profile.yaml locally | Operator profiles + organizational baselines compose bidirectionally |
| **Event sink** | Local newline-delimited JSON | Central event ingestion + local mirror |
| **Enforcement** | Flag only; `on_match` vocabulary reserved but inactive | Real-time enforcement: `block`, `deny`, `prompt`, scope contraction |
| **Memory** | Within-session only | Organizational memory; longitudinal drift; cross-session profile-fingerprint correlation |
| **Compliance surfaces** | None | Audit trails, policy distribution, technical documentation surfaces supporting EU AI Act Article 12 logging and SOC 2 work |
| **Agent registry** | None | First-class component; fleet visibility |
| **Network** | No outbound calls on the governance path | Sentience-hosted OR customer-hosted control plane |
| **Account** | None required | Required for control-plane tier |

**Same schema. Same control points. Open tier surfaces. Paid tier enforces.** Upgrade from open to paid is a deployment decision, not a rebuild. Open-tier events are structurally identical to the events the paid control plane consumes for enforcement, organizational memory, and compliance work.

Everything required to govern one operator on one machine ships in the open tier, permanently, under Apache 2.0.

---

## Comparison with Other Tools

### Observability tools (LangSmith, Helicone, Langfuse)

LangSmith and similar observability platforms are strong tools for what they're designed to do: record agent traces, surface latency, and give engineers a debugger-shaped view of what their agent did. Operators using them get high-fidelity playback of execution.

Sentience operates one architectural layer down. The two are complementary, not competitive.

| | Observability | Sentience |
|---|---|---|
| **Primary job** | Record what happened | Compare what happened to what was declared |
| **Data captured** | Prompts, completions, tool calls, latencies | Structured metadata only (no prompts, no completions) |
| **Reads against** | Free-form trace navigation | Operator-authored governance profile |
| **Output shape** | Searchable trace history | Local evidence stream + advisory flags + simulated consequences |
| **Compliance surface** | Vendor-side privacy model | Local-first; by default nothing leaves the machine |
| **Where it sits** | Captures the call, often via proxy | Sits beside the runtime, never inline |

Operators running observability tools and Sentience together get both surfaces. Observability tells you what happened. Sentience tells you whether it matched declared intent.

### LLM gateways (Bifrost, LiteLLM, OpenRouter wrappers)

Gateways like Bifrost solve a real operator problem: routing across providers, cost control, failover, key management. They're a legitimate piece of infrastructure for any multi-provider agent stack.

Gateways operate in the request path. Sentience does not. Different responsibilities, different failure modes.

| | LLM Gateway | Sentience |
|---|---|---|
| **Primary job** | Route the request | Govern the action |
| **Position** | In the call path between agent and provider | Beside the runtime, observing |
| **Failure mode** | Request fails if gateway is down | Agent continues to work; only governance evidence is lost |
| **What it captures** | Request/response metadata for routing decisions | Structured action metadata for declared-intent comparison |
| **Operator concern** | Provider sprawl, cost, reliability | What the agent actually did, against what was asked |

An operator can run a gateway for routing and Sentience for governance simultaneously. Neither displaces the other.

### LLM-judge patterns (Constitutional AI, LLM-as-evaluator)

LLM-judge patterns ask one LLM to evaluate another LLM's output. They are useful for content quality assessment and certain alignment work. They are not deterministic, they are not cheap at scale, and they require trusting the judge LLM's own behavior.

Sentience does not use LLM-as-judge anywhere in the policy evaluation path. Policy logic is pure structured-metadata evaluation. Same input always produces the same flags. No second model in the loop.

| | LLM-as-judge | Sentience |
|---|---|---|
| **Evaluation logic** | Inference by a second LLM | Deterministic logic over structured metadata |
| **Cost per evaluation** | Token cost of the judge LLM | Negligible (regex match + dict lookup) |
| **Reproducibility** | Stochastic; reruns vary | Byte-identical reruns |
| **Failure mode** | Judge LLM fails or hallucinates | Wrapper crash blocks evaluation but does not break the agent |

LLM-as-judge has legitimate use cases. Governance at the execution boundary is not one of them.

---

## What v0.2.5 is not

Two boundaries hold for every release:

- **Declared intent is untrusted input.** Sentience can identify when captured
  actions diverge from what an agent declared. It cannot determine whether the
  declaration itself was truthful or complete, or infer the agent's underlying
  motives. Recording an unsafe action correctly does not make the action safe or
  the agent trustworthy.
- **Governs supported agent actions, not model behavior.** It evaluates
  observable agent actions in business and operational workflows. It does not
  detect bias, toxicity, hallucinations, harmful content, or other
  model-output and content-safety issues.

Honesty about scope limits is load-bearing.

- **Not enforcement.** Open tier ships observability and advisory flagging. The `on_match` vocabulary (`block`, `prompt`, `deny`) is reserved in the schema and defaults to `flag` in the open tier. Enforcement activates in the paid control plane.
- **Not cross-session memory.** Each session evaluates against the profile in isolation. The profile_fingerprint carrier exists today; longitudinal drift detection across sessions ships in v0.3.x.
- **Not cross-operator policy.** The profile is per-machine, per-operator. Organizational policy distribution that composes with operator profiles is the v0.4.x scope, in the paid control plane.
- **Not a hosted dashboard.** The CLI is the surface. The analyzer is the renderer. The operator's editor is the authoring tool. The paid tier introduces the operator console, not a hosted dashboard.
- **Not a content-inspection layer.** Sentience never reads prompts or completions. If a use case requires content-level inspection, Sentience is the wrong tool.
- **Not a runtime modification.** The wrapper observes; it does not change the agent's control flow. If the agent does something the operator does not want, Sentience surfaces evidence but does not prevent the action in the open tier.

---

## Further reading

Repository:
- [README.md](../README.md) — quickstart and install
- [CHANGELOG.md](../CHANGELOG.md) — release history (v0.2.5 governance profile, v0.2.5.1 first-run copy patch)
- [docs/guide/sentience_governor.md](./guide/sentience_governor.md) — user documentation
- [examples/showcase/v025-closed-loop/](../examples/showcase/v025-closed-loop/) — runnable end-to-end example

Substack:
- [Great Expectations](https://sentientnotes.substack.com/p/great-expectations) — v0.2.5.1 release essay (May 2026)
- [sentientnotes.substack.com](https://sentientnotes.substack.com) — full Sentient Notes archive
