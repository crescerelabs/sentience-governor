# MCP integration

`wrap_mcp_client` produces a governed MCP client that emits a
governance event around every tool call. Governance profiles are
picked up automatically: no signature changes, no new keyword
arguments.

## How profiles plug in

Inside `_WrappedMCPSession._start()`, the wrapper resolves the profile
for its `agent_id` once, with `resolve_profile(agent_id=...)`: the first
matching binding in `~/.sentience/resolution.yaml` if there is one, else
`~/.sentience/profile.yaml` if it exists, else none (0.3.2; a matched
binding that fails to load is `degraded` and falls to the default without
consulting a later binding). The result goes to
`SessionManager.session_start(profile=...)` and the resolution provenance
to the `AGENT_REGISTERED` event. One process is one session, so the
profile is sticky for the session by construction. When nothing resolves,
the session takes the pre-profile code path.

MCP tool calls are **not** semantically classified in 0.3.2 (that is a
Claude Code Bash feature): `high_consequence.tools` patterns apply to
them, `high_consequence.operations` rules do not.

## Mapping your existing setup

| If you pass to `wrap_mcp_client` ... | The profile controls ... |
| :-- | :-- |
| `stated_objective="..."` | how `INTENT_DECLARED` populates. The profile's `demand_at` decides what happens when no objective was supplied. |
| `classification_hook=...` | nothing — classification metadata flows through the same `CONTEXT_SNAPSHOT` and `MEMORY_WRITE_ATTEMPT` events as before. |
| Your MCP tool names | which `SCOPE_ASSERTED` events fire `HIGH_CONSEQUENCE_DETECTED`. Patterns in `high_consequence.tools` are matched against `<tool_id>:<target_system>` — usually the MCP tool name plus whatever the wrapper inferred as the target. |

## What the trace looks like under a profile

Every event carries an envelope-level `profile_fingerprint`
(12 hex chars). The `AGENT_REGISTERED` event additionally carries
`profile_loaded: true` and `profile_schema_version` in its payload, and,
when the session resolved through a binding, `profile_resolution`
(`bound` or `degraded`) and `profile_binding` (the matched pattern).
The new advisory flags (`TASK_BOUNDARY_CROSSED`,
`HIGH_CONSEQUENCE_DETECTED`) fire on `SCOPE_ASSERTED` events when
the profile's signals trigger.

## Regex tips for `high_consequence.tools`

The composite the wrapper matches against is
`f"{tool_id}:{target_system}"`. Examples that work in practice:

```yaml
high_consequence:
  tools:
    - "Bash:.*rm.*-rf.*"            # dangerous rm under a Bash tool
    - "fs.write:.*\\.env.*"         # any .env file write
    - "db.delete:.*production.*"    # production-scoped deletes
```

Test your regexes against a representative trace before relying on
them in production. The wrapper validates the profile at load time
but does not validate every regex against synthetic inputs — that's
your job during profile authoring.

## What this integration does NOT do

- It does not block tools. Profiles in the open tier are
  observability — matched events get flagged in the trace; the call
  proceeds.
- It does not change `wrap_mcp_client`'s signature. Existing MCP
  integrations work unchanged; adding a profile only enriches the
  trace.

For the full MCP integration walkthrough see §5 of the
[user guide](../guide/sentience_governor.md).
