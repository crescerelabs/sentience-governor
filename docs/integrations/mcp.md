# MCP integration

`wrap_mcp_client` produces a governed MCP client that emits a
governance event around every tool call. v0.2.5 adds governance
profiles: if `~/.sentience/profile.yaml` exists, the wrapper picks
it up automatically — no signature changes, no new keyword
arguments.

## How profiles plug in

Inside `_WrappedMCPSession._start()`, the wrapper calls
`GovernanceProfile.from_default_path_or_none()` and passes the
result to `SessionManager.session_start(profile=...)`. When the file
is absent, the session takes the v0.2.4 code path; when it exists,
the wrapper enforces the profile's signals.

## Mapping your existing setup

| If you pass to `wrap_mcp_client` ... | The profile controls ... |
| :-- | :-- |
| `stated_objective="..."` | how `INTENT_DECLARED` populates. The profile's `demand_at` decides what happens when no objective was supplied. |
| `classification_hook=...` | nothing — classification metadata flows through the same `CONTEXT_SNAPSHOT` and `MEMORY_WRITE_ATTEMPT` events as before. |
| Your MCP tool names | which `SCOPE_ASSERTED` events fire `HIGH_CONSEQUENCE_DETECTED`. Patterns in `high_consequence.tools` are matched against `<tool_id>:<target_system>` — usually the MCP tool name plus whatever the wrapper inferred as the target. |

## What the trace looks like under a profile

Every event carries an envelope-level `profile_fingerprint`
(12 hex chars). The `AGENT_REGISTERED` event additionally carries
`profile_loaded: true` and `profile_schema_version` in its payload.
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

For the full MCP integration walkthrough see §5 of the user guide
at `userdocs/sentience_governor.md`.
