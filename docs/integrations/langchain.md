# LangChain integration

Sentience Governor's LangChain support is two complementary objects
that wrap any LangChain agent:

- `SentienceCallbackHandler` — drop-in callback that hooks
  `on_chain_start`, `on_llm_start`, `on_tool_*`, and `on_chain_end`
  to emit governance events.
- `SentienceMiddleware` — agent middleware that wraps tool calls and,
  optionally, LangGraph steps, so tool activity is governed without
  wiring the callback yourself.

## Sessions and concurrency

**One run, one session.** A single agent invocation produces a single
governance session, including under LangGraph, where the framework
fires chain-level callbacks once for the graph and once for every node.
Nested structure is recorded as structure, not as extra sessions.

That is what keeps the trace honest about you: each session carries its
own declared-intent baseline, so a tool inside your declared
capabilities is evaluated against that baseline however deeply the
graph nests it. A tool you never declared still reports POL-001.

**One handler can serve overlapping runs.** Two invocations in flight at
once — on threads or on one event loop — get separate sessions, and
neither can pick up the other's token usage, model, provider or turn
id. The same isolation holds between parallel branches inside a single
graph.

Two limits to know. `SentienceMiddleware` gets no run identifiers from
LangChain, so it remains **one middleware instance per agent run**; with
more than one run open it reports a governance error rather than
guessing which run a tool call belongs to. And a tool event that cannot
be traced to a known run is skipped rather than filed under an
arbitrary session.

For the mechanics behind this, see §6 of the
[user guide](../guide/sentience_governor.md).

## How profiles plug in

If `~/.sentience/profile.yaml` exists when a chain starts, the
handler loads it transparently and the session runs under it. No
keyword arguments change; no constructor flags need adjusting.

You don't import `GovernanceProfile` directly. The handler calls
`GovernanceProfile.from_default_path_or_none()` internally — when the
file is absent, the session takes the v0.2.4 code path; when it
exists, the wrapper enforces the profile's signals.

## Mapping your existing setup

| If you set ... | The profile controls ... |
| :-- | :-- |
| `stated_objective="..."` on the handler constructor | how `INTENT_DECLARED` populates. The profile's `demand_at` decides what happens when no objective was supplied. |
| `agent_id="..."` / `vendor_id="..."` | nothing — these continue to populate `AGENT_REGISTERED` unchanged. v0.2.5 adds optional `profile_loaded` + `profile_schema_version` to that event when a profile is active. |
| LangChain tools registered on your agent | which `SCOPE_ASSERTED` events fire the new advisory flags. Patterns in `high_consequence.tools` are matched against `<tool_id>:<target_system>`. |

## What the trace looks like under a profile

Each event carries an envelope-level `profile_fingerprint` (12 hex
chars). On the `AGENT_REGISTERED` event the payload additionally
carries `profile_loaded: true` and `profile_schema_version`. The
new advisory flags appear on `SCOPE_ASSERTED` events when the
profile's signals trigger.

Existing analyzers that don't recognize the new flag values
continue to work — they list the values as unknown strings in
`advisory_flags` and ignore them. The schema is forward-compatible.

## What this integration does NOT do

- It does not block tools or modify their arguments. Both
  `SentienceCallbackHandler` and `SentienceMiddleware` are pass-
  through; the trace records what happened.
- It does not require any changes to your existing agent
  construction. Existing LangChain applications work unchanged when
  a profile is added; the only difference is the trace gets richer.

For the full LangChain integration walkthrough (intent declaration,
classification metadata, etc.) see §6 of the
[user guide](../guide/sentience_governor.md).
