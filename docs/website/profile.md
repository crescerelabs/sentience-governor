# Governance Profiles

A governance profile is an operator-authored YAML file at
`~/.sentience/profile.yaml` that encodes what you expect of any
agent on this machine. It's the machine-readable sibling to the
agent's `CLAUDE.md` recipe — same expectations, two surfaces.

## What a profile shapes

In v0.2.5, a profile controls three things — all of them
observability signals, not enforcement:

1. **When undeclared intent is surfaced** — `session_intent.demand_at`
   takes one of `session_start`, `first_write`, or `never`. The
   wrapper fires `POL-001` per your choice.
2. **When a task boundary has been crossed** —
   `task_boundary.signals` is a list of any of `dir_change`,
   `file_type_shift`, `time_gap`, `read_to_write_transition`. The
   wrapper attaches `TASK_BOUNDARY_CROSSED` to the next
   `SCOPE_ASSERTED` when a signal fires.
3. **Which tools should be treated as high-consequence** —
   `high_consequence.tools` is a list of regex patterns matched
   against `<tool_id>:<target_system>`. Matches attach
   `HIGH_CONSEQUENCE_DETECTED` to that event.

## Quickstart

```bash
sentience profile init       # create starter profile (inline-commented)
sentience profile view       # inspect it
sentience profile edit       # tune it ($VISUAL/$EDITOR, else nano/vim/vi, else macOS TextEdit)
sentience profile validate   # schema check (read-only)
```

Sessions started after the file exists pick it up automatically —
no environment variable, no flag. Every event in a governed session
carries `profile_fingerprint` at the envelope level so traces
correlate back to the profile that produced them.

## Example

A complete runnable walkthrough — profile + agent recipe + generated
trace + generated analyzer report — lives in the source tree at
`examples/showcase/v025-closed-loop/`.

The analyzer's Markdown report gains three optional sections when a
profile is active:

- `## Profile` — fingerprint + schema version
- `## High-consequence operations` — table of matched tools per turn
- `## Task boundaries crossed` — table of boundary-crossing events

These sections are omitted when no profile metadata is present, so
v0.2.4-shaped traces produce byte-identical reports under v0.2.5.

## Integration

Profiles are transparent to every wrapper surface:

- **Claude Code hook** — picks up the profile automatically on every
  session start.
- **MCP wrapper** — see [MCP integration](./integrations/mcp.md).
- **LangChain handler / middleware** — see
  [LangChain integration](./integrations/langchain.md).

## What this is not

Profiles in the open tier are observability, not enforcement. The
schema reserves vocabulary (`on_match: prompt | block | deny`) for
future paid-tier behavior, but in v0.2.5 every `on_match` value
warns and falls back to `flag`.
