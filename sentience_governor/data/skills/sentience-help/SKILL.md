---
name: sentience-help
description: |
  Explains the available Sentience slash commands, what they do,
  and the boundaries (latest session only; no historical browsing).
disable-model-invocation: true
---

# Sentience slash commands

## What you can do

- `/sentience-pulse` — one-command report for the latest captured Claude Code session
- `/sentience-status` — is the hook capturing?
- `/sentience-profile` — what governance rules are active?
- `/sentience-violations` — per-rule policy-violation drill-down
- `/sentience-intent` — per-turn intent-drift drill-down
- `/sentience-review` — retrospective review of your existing Claude Code history

## What you can't do (and where it lives instead)

- **No historical session browsing.** Session-bound slash commands
  (`/sentience-pulse`, `/sentience-violations`, `/sentience-intent`)
  operate on the latest captured session. Use `sentience open
  <session_id>` in your terminal for prior sessions.
- **No cross-session aggregation.** That's a future control-plane /
  paid-tier capability.
- **No Claude-initiated invocation.** Claude can't run these on its
  own in this release; structured Claude-initiated governance ships in
  the MCP release.
- **The latest _captured_ session may differ from the session you're
  typing in.** These commands run at skill-preprocessing time and read
  the most recently *captured* session via the local CLI. A brand-new
  live session has no captured token data until it ends.

## Token capture

If `/sentience-pulse` shows `no_signal`, the `SessionEnd` hook
probably isn't wired. Run `sentience init claude-code` to wire it,
then start a new Claude Code session.

## Requires

These skills shell out to the local `sentience` binary. If you copied
them from a skill directory, install the package first:

```
pipx install sentience-governor
sentience init claude-code
```
