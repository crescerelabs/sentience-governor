# Sentience user documentation

> **Looking for the project overview?** → **[root `README.md`](../../README.md)** — PyPI install, quickstarts, CLI commands table, Claude Code hook setup, LangChain + MCP integration. That's the landing page.
>
> This directory holds the deep-dive operator guide for **Sentience Governor** (the runtime). Start at the root README first, then [the docs index](../index.md); come here when you want the full manual.

---

User-facing guides for the two components shipped in this distribution:

- **[`sentience_governor.md`](./sentience_governor.md)** — Sentience Governor, the runtime that wraps your agent and produces governance traces.

These docs are for **operators and integrators** — people who want to use the package. For the architecture, see [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md).

## Which doc do I read?

| Question | Read |
| :-- | :-- |
| "What does this package do?" | [`sentience_governor.md`](./sentience_governor.md) §1–2 |
| "How do I wrap my agent?" | [`sentience_governor.md`](./sentience_governor.md) §5–6 |
| "How do I see what my agent did?" | [`sentience_governor.md`](./sentience_governor.md) §8 (the CLI viewer) |
| "Can I run a real demo with Claude?" | [`examples/README.md`](../../examples/README.md) |

## Project status

**v0.3.0.2** (Apache 2.0). Install:

```bash
pip install sentience-governor
```

For the opt-in MCP server (governance tools Claude can call), install the
extra:

```bash
# virtualenv / pip
pip install "sentience-governor[mcp]"

# pipx-managed install (ambient pip cannot reach the pipx venv)
pipx install --force "sentience-governor[mcp]"
```

Or from source for development:

```bash
git clone https://github.com/crescerelabs/sentience-governor.git
cd sentience-governor
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Sentience is local-first by default. The experimental **Sync cloud telemetry** CLI was sunset in v0.2.8.3 and its `sentience-sync` command was removed in v0.3.0.1.

## What this distribution contains

```
sentience_governor/   # the runtime — wraps agents, emits governance events
examples/             # standalone demo scripts (NOT in the wheel)
docs/                 # all documentation, incl. this guide (NOT in the wheel)
tests/                # passing test suite (NOT in the wheel)
```

The three CLI commands installed by `pip install`:

- **`sentience`** — curated viewer for agent-hook session traces (Claude Code today): `sentience status`, `sentience list`, `sentience open [--latest | <id>] [--summary]`
- **`sentience-cli`** — raw viewer for library traces (MCP wrapper, LangChain, golden-trace fixtures)
- **`sentience-claude-code-hook`** — invoked by Claude Code via `.claude/settings.json`; not run by operators directly

Run any of them with `--help` for built-in usage. Or read the deep-dive guides above. The root `README.md` has a compact commands table if you just need the skim.

## Quick orientation in 30 seconds

The Sentience Governor sits between your agent and its tools. When the agent calls a tool, the Governor emits a structured governance event describing what happened — who the agent is, what it intended, what tool it called, what data came back, what it tried to persist. These events accumulate as a trace you can read with `sentience-cli`.

The former **Sync cloud telemetry** CLI was sunset in v0.2.8.3 and its `sentience-sync` command was removed in v0.3.0.1. (The separate "Sentience Sync" email list at `getsentience.ai/sentience-sync` is unrelated and still available.)

## Feedback

This is a pre-release. If anything in these docs is wrong, unclear, or out of date, file feedback through the same channel you're already using to talk to the Sentience team.
