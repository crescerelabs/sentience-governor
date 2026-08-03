# Sentience Governor — Documentation

Local-first governance for AI agents. Connect Claude Code, run `sentience pulse`, and get a shareable report of agent actions, tool calls, intent drift, policy violations, and per-turn token burn.

Installs from PyPI with one command. No account required. Nothing is sent automatically.

## Start here (2 minutes)

1. Install → `pipx install sentience-governor`
2. Wire Claude Code → `sentience init claude-code`
3. Run Claude Code normally
4. Inspect the session → type `/sentience-pulse` inside Claude Code, or run `sentience pulse --latest` in a terminal

Inside Claude Code, type `/sentience-help` or `/sentience-pulse` — `sentience init claude-code` installs the Claude Code skills for you (new in 0.2.8). See [Quickstart](./quickstart.md).

Everything else can wait.

## What you get after a run

Sentience turns an agent session into a local pulse:

- What the agent did
- Which tools ran, and which kinds of work rode the expensive turns
- Where it drifted from declared intent
- Which policy rules fired
- Which turns carried the most token/context burn
- What deserves inspection before you trust the output

Token burn means token/context footprint, not dollar cost.

## Definitions

Session = one run of an agent
Event = one action inside a session
Trace = all events for a session

## Where to go

- **[Install](./install.md)** — get the CLI on your machine.
- **[Quickstart](./quickstart.md)** — from zero to your first trace in three minutes.
- **[Commands](./commands.md)** — reference for every CLI command.
- **[Cloud telemetry sunset](./sync-privacy.md)** — what changed in v0.2.8.3 and what remains local.
- **[Troubleshooting](./troubleshooting.md)** — common issues and their fixes.
- **[Changelog](./changelog.md)** — what changed, and when.

### CLI tools you get

- `sentience` → view agent sessions and run `pulse`
- `sentience-cli` → raw trace viewer
- `sentience-sync` → sunset stub for the removed Sync cloud telemetry CLI
- `sentience-claude-code-hook` → Claude Code hook entry point

## A governance runtime

Sentience wraps your agent at the execution boundary with five control points:

`AGENT_REGISTERED`, `INTENT_DECLARED`, `SCOPE_ASSERTED`, `CONTEXT_SNAPSHOT`, `MEMORY_WRITE_ATTEMPT`.

It emits a structured event at each control point. Each event is evaluated against five default policy rules. Violations are annotated with the consequence real-time enforcement would apply — enforcement itself is planned for a future paid tier.

Nothing gets blocked. Everything is visible.

## Claude Code support

For Claude Code, `sentience init claude-code` wires the local hook into your project and installs six `/sentience-*` skills. The hook observes tool calls, records governance events, and captures per-turn token burn from the session transcript.

That lets `sentience pulse` connect tool activity, policy signals, intent drift, and token burn in one report. Inside Claude Code, `/sentience-pulse` renders that report inline without switching to a terminal.

The in-chat commands are operator-invoked, latest-session only, and read-only.

## Trace files

Claude Code session traces land at `~/.sentience/traces/claude-code/`. You can read, grep, diff, replay, or feed them into your own analysis.

## Sync CLI

`sentience-sync` is a sunset stub for the removed Sync cloud telemetry CLI. See [Cloud telemetry sunset](./sync-privacy.md).

## The philosophy

- **Observe-only.**
- **Fail-open.**
- **No automatic network calls.**

## Who it is for

- **Claude Code users** who want a local flight recorder for agent sessions.
- **Operators** running AI agents who need an audit trail.
- **Teams adopting MCP or LangChain** who need structured logs of tool-call behavior.
- **Developers evaluating governance** who want to see the open-tier surface before enforcement.

## What the open tier does not do

Real enforcement — block, pause, scope contraction, retroactive revocation — is planned for a future paid tier. The open tier is the observability half of the same architecture.

The upgrade path is additive. Adopting open-tier today puts you on the path to full enforcement later.

---

[Install →](./install.md)

Stuck? → [Troubleshooting](./troubleshooting.md)
