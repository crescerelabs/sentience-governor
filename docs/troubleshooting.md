# Troubleshooting

## Quick checks (try these first)

```bash
sentience status
sentience list
sentience open --latest
```

## Most common issue

Hook not firing → check PATH or restart Claude Code.

---

## Install

### `pip install sentience-governor` fails with `externally-managed-environment`

Modern macOS and Linux Pythons block `pip install` outside a virtualenv (PEP 668). Use `pipx` instead:

```bash
pipx install sentience-governor
```

### `pipx install` fails with `ensurepip` exit status 1

pipx defaulted to a Python with a broken `ensurepip`. Force a known-good version:

```bash
PIPX_DEFAULT_PYTHON=python3.12 pipx install sentience-governor
```

### `pipx` is not installed

```bash
brew install pipx          # macOS
# or:
python3 -m pip install --user pipx   # Linux / WSL
pipx ensurepath
```

Restart your shell after `pipx ensurepath`.

### `sentience: command not found` after install

Run `pipx ensurepath`, then restart your shell. The pipx bin directory needs to be on your `$PATH`.

### Python version too old

Sentience Governor requires Python 3.10 or newer. Confirm with `python3 --version`. If older, install a newer Python (`brew install python@3.12`).

---

## Claude Code hook

### `sentience status` says hook is not firing

Cause:
One of: wrong settings file, CLI not on PATH, Claude Code started before you saved the config, or no tool calls happened in the session.

Fix:
1. Confirm config location: the machine-local `.claude/settings.local.json` (0.3.0.3+ canonical home; Claude Code v2.1.211+ resolves it at the repository root). A `.claude/settings.json` entry from an older release still works while its path exists, but is treated as read-only legacy — running any `sentience` command in the project migrates a dead one to `settings.local.json` automatically.
2. Confirm CLI resolves: `which sentience-claude-code-hook` from Claude Code's launch shell.
3. Restart Claude Code — it reads settings at startup.
4. Make actual tool calls — the hook only fires on tool invocations.

### `/sentience-pulse` does not appear in Claude Code

Cause:
The skills directory was created after Claude Code started, or skills were not installed.

Fix:
1. Run `sentience init claude-code` (without `--no-skills`).
2. Restart Claude Code, then type `/sentience-help`.
3. If you installed project-local skills with `--project`, make sure Claude Code trusts that workspace.

### `/sentience-pulse` says `sentience: command not found`

Cause:
The slash command shells out to the local `sentience` binary, but Claude Code cannot find it on `$PATH`.

Fix:
1. Confirm `sentience --version` works in a fresh terminal.
2. If you installed with pipx: `pipx ensurepath`, restart your shell, then restart Claude Code.
3. Re-run `sentience init claude-code` — it warns at install time if `sentience` is not resolvable.

### `/sentience-pulse` reports `no_signal` in a running session

Cause:
This is expected, not a failure. Per-turn token data is written when the Claude Code session **ends** — a still-running session has events but no turn/token records yet.

Fix:
End the session and run the pulse again (or run it against a previously ended session). Since v0.2.8.2, when the latest session has no token data the pulse **shows your most recent session that *does*** — with a transparent header naming it — so you don't have to hunt for it (resuming a conversation mints a new session id, so the newest one is often an empty live segment). An explicit `sentience pulse <id>` is honoured exactly. If a session that has **ended** still reports no signal, run `sentience init claude-code` and start a new session.

### `sentience open --latest` shows no events, but the hook fired

Cause:
The curated viewer is hiding baseline-noise events in `--summary` mode.

Fix:
```bash
sentience-cli ~/.sentience/traces/claude-code/<session-id>.jsonl
# Or drop --summary:
sentience open <id>
```

---

## Sync cloud telemetry

The experimental Sync cloud telemetry CLI was removed in v0.2.8.3. `sentience-sync` now prints a local-first notice and exits 0. Your local pulse, status, profile, and Claude Code capture are unaffected.

If you are trying to join the product update list, use the Sentience Sync email list on the website. That list is separate from the removed telemetry CLI.

---

## Claude Code vs. library-integrated — which viewer?

- **Claude Code sessions** → `sentience` (curated).
- **MCP wrapper / LangChain callback** → `sentience-cli` (raw).

---

## Still stuck?

Reach out via support on [getsentience.ai](https://getsentience.ai).

Include:
- The exact CLI command that failed
- Full stderr output
- OS and Python version
