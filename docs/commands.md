# Commands

## Most used

Inside Claude Code:

```text
/sentience-pulse
/sentience-status
```

From a terminal:

```bash
sentience pulse --latest
sentience open --latest --summary
sentience list
```

## Inside Claude Code (slash commands)

New in 0.2.8: `sentience init claude-code` installs six Claude Code skills, exposed as `/sentience-*` slash commands — read governance signals without leaving the chat.

| Command | What it shows |
| :-- | :-- |
| `/sentience-help` | What the commands do and their boundaries |
| `/sentience-pulse` | One-command report for the latest captured session |
| `/sentience-status` | Is the hook capturing? (policy violations vs advisory flags, split) |
| `/sentience-profile` | The active governance profile (view-only) |
| `/sentience-violations` | Per-rule policy-violation drill-down |
| `/sentience-intent` | Per-turn intent-drift drill-down |

Each command is a thin wrapper over the matching `sentience` CLI command below: the CLI produces the deterministic numbers at preprocessing time, and Claude renders them inline and is instructed to show them verbatim. Anything Claude adds beyond the rendered report is interpretation, not Sentience measurement.

The slash surface is deliberately bounded: **operator-invoked only** (Claude can't run them on its own), **latest captured session only** (no history browsing or cross-session aggregation), and **read-only** (nothing mutates your governance, session, or remote state). Per-turn token data is written when a session *ends* — running `/sentience-pulse` inside a still-running session reports `no_signal` with that explanation.

Install via [`sentience init claude-code`](#sentience-init-claude-code-path) (skills land in your personal `~/.claude/skills/`; `--project` makes them project-local; `--no-skills` opts out).

## Reference

Four CLI entry points registered by `pip install sentience-governor`.

| Command | When to use |
| :-- | :-- |
| [`sentience`](#sentience) | Reviewing Claude Code session traces, plus running derived-metric analyzers (e.g. `sentience analyze undeclared-intent`) |
| [`sentience-cli`](#sentience-cli) | Reviewing library-integrated agent traces |
| [`sentience-claude-code-hook`](#sentience-claude-code-hook) | Claude Code invokes this; you do not run it directly |

---

## `sentience`

Curated viewer for agent-hook session traces.

**First run:** the first time you invoke `sentience` after install, you'll see a one-time prompt asking if you want to know when the hosted console ships. Drop your email or hit Enter to skip — it never asks again. Optional. Drop email at [getsentience.ai/launch-list](https://getsentience.ai/launch-list/) any time.

### `sentience status`

Reports whether the hook is capturing anything and prints a curated summary of the most recent session.

```bash
$ sentience status
Sentience Status

Hook:           sessions detected
Trace path:     ~/.sentience/traces/claude-code

Last session:
  ID:                 f41ee94f-f686-48c7-8107-8df2749c2a15
  Time:               2026-05-06 21:55
  Events:             4712
  Policy violations:  2710
  Advisory flags:     2717

Sample event:
  Bash → run("pytest tests/test_claude_code_hook.py")

Sentience is governing your Claude Code sessions locally.
```

Policy violations and advisory flags are reported separately (new in 0.2.8). Add `--json` for a machine-readable view that also exposes the count reconciliation: per-category totals, baseline-filtered codes, and the raw total (`raw_total = policy + advisory + baseline_filtered_total`).

From 0.3.0.4, the reported session is your newest one with recorded activity. Empty sessions (a Claude Code restart could leave one behind before 0.3.0.4) are passed over, and the report says how many; `--json` lists them under `transient_sessions`. If every session on disk is empty, the newest is shown and labelled.

### `sentience list`

One line per session, newest first, max 20.

```bash
$ sentience list
Sentience Sessions

 1. a7f3b1c2-d4e5     2m ago    32 events   ⚠ 12v/15a
 2. e91b8a7f-c3d2     4h ago   128 events   ✓ 0v/0a
```

The glyph splits the counts: `Nv` policy violations / `Ma` advisory flags (new in 0.2.8). From 0.3.0.4, a session with no recorded activity is labelled `transient — no activity` in place of the glyph.

### `sentience init claude-code [path]`

> **Requires Claude Code v2.1.211 or later** (from sentience-governor 0.3.0.3).
> The hook binding is written to the machine-local `.claude/settings.local.json`,
> which Claude Code resolves at the repository root from that version onward.
> Sentience cannot reliably determine which Claude Code version will read this
> configuration; on older versions the integration may silently capture nothing.

Wires the Claude Code hook into a project's machine-local
`.claude/settings.local.json` (from 0.3.0.3; earlier releases wrote the shared
`.claude/settings.json`) so every tool call in that project emits a governance
event — and installs the six `/sentience-*` slash commands (new in 0.2.8).

```bash
sentience init claude-code             # wire the current directory + install skills
sentience init claude-code /path/to/project   # wire a specific project
sentience init claude-code --no-skills        # hooks only
sentience init claude-code --project          # skills into <project>/.claude/skills/
sentience init claude-code --force            # overwrite a hand-edited skill
sentience init claude-code --mcp              # also register the MCP server (new in 0.3.0)
```

Idempotent by convergence — the machine-local `.claude/settings.local.json` is brought to exactly one Sentience hook entry per event for the running install: a stale binding from a removed or moved install is updated, a partial historical install is completed, duplicates collapse to one, and an already-current configuration is a no-op. Hooks that aren't Sentience's are never touched, and the team-shared `.claude/settings.json` is never written: legacy Sentience entries there are treated as read-only migration evidence (coordinate their removal with your team). A hand-customised Sentience-looking entry is never rewritten automatically — init reports it and stops. Re-running refreshes skills safely: an unchanged skill is a no-op, a new release updates managed skills, and a skill you've hand-edited is preserved unless `--force` (tracked via a per-root `.sentience-skills.json` manifest). By default hooks are wired for the initialized project while skills install to your personal `~/.claude/skills/`. It resolves and verifies the correct hook-binary path for your install (pipx, pip-in-venv, or source), so you don't hand-edit JSON or guess a path — and it warns (never fails) if `sentience` isn't resolvable on your `$PATH`.

From 0.3.0.3, any other `sentience` command run inside an already-configured project also brings that project's Sentience-managed hook configuration up to date for the running install (upgrade/reinstall recovery). This never configures a project that has no Sentience configuration, never touches the shared `settings.json`, and never blocks the command you ran — if the configuration can't be updated safely, you get one warning line on stderr instead.

`--mcp` (new in 0.3.0) additionally registers the [Sentience MCP server](#sentience-mcp-server) into the project's `.mcp.json` and prints a consent notice. It is opt-in: without `--mcp`, nothing MCP-related is registered. Needs the server extra, installed into the same environment as the CLI: `pip install "sentience-governor[mcp]"` in a virtualenv, or `pipx install --force "sentience-governor[mcp]"` for a pipx-managed install.

### `sentience-mcp-server`

The opt-in MCP server (new in 0.3.0) exposes seven governance tools Claude can call inside a session: `sentience_explain` and `sentience_profile_view` (session-independent reads), `sentience_pulse` / `sentience_intent` / `sentience_violations` (reads of the **last completed** session, each naming the session it read), `sentience_session_status` (the live session, **structural-only**: no token / burn / pulse figure, since token analysis is unavailable until SessionEnd), and `sentience_declare_intent(objective, scope)` (the one forward-looking write). It is stdio, local, no HTTP, no auth, and never registered by default.

A `declare_intent` event is server-written, append-only, and **non-retroactive**: matching activity after it stops firing POL-001 at capture, while pre-declaration events keep theirs. It is recorded as `intent_source = inferred` (agent-declared through MCP, so its content is untrusted and **not** integrator-vouched; never read it as an operator endorsement). It fails closed on any uncertain session binding.

Register it with `sentience init claude-code --mcp`. To see the BEFORE/AFTER POL-001 flip a declaration produces without wiring anything, run `sentience demo declare-intent`, a deterministic synthetic showcase (it does not spawn the server or exercise live session identification).

### `sentience open [--latest | <id> | <path>] [--summary]`

Renders a single session.

```bash
sentience open --latest              # full render
sentience open --latest --summary    # one-screen view
sentience open a7f3                  # by id prefix
sentience open /path/to/session.jsonl  # by trace file path (since v0.2.5.5)
sentience open a7f3... --summary     # explicit id + summary
```

`--latest` resolves the most recently *started* session (stable: an actively-running session won't reorder results between commands).

```
Summary → what happened
Focus → anomalies
Key Events → important moments
Full Trace → everything
```

### `sentience analyze undeclared-intent [target] [flags]`

Shows how much compute in a session was attributed to reasoning turns that touched execution outside the session's declared operational intent. Available since v0.2.4.

```bash
sentience analyze undeclared-intent --latest                   # most recent session
sentience analyze undeclared-intent 7f3b                       # session-id prefix
sentience analyze undeclared-intent /path/to/session.jsonl     # explicit file
sentience analyze undeclared-intent --showcase                 # bundled example (since v0.2.5.5)
sentience analyze undeclared-intent --latest --json            # structured output
sentience analyze undeclared-intent --latest --save            # write report, no prompt
sentience analyze undeclared-intent --latest --no-prompt       # no save prompt
```

Flags:

| Flag | Behavior |
| :-- | :-- |
| (positional `target`) | Session id (or prefix), or a path to an `.jsonl` trace file. |
| `--latest` | Use the most recently captured session. |
| `--showcase` | Analyze the bundled example trace — a populated analysis you can run on a fresh install before token attribution is wired. (since v0.2.5.5) |
| `--json` | Emit structured JSON instead of human-readable output. |
| `--save` | Skip the prompt and write the Markdown report directly. |
| `--no-prompt` | Disable the post-render save prompt entirely. |

Status values: `ok` (full attribution), `partial` (analysis completed, but some events could not be fully attributed), `no_token_data` / `no_turns` (no per-turn token data yet — usually a still-running session, since per-turn data is written when the session ends). The save flow is suppressed for non-`ok` statuses.

When no intent declaration exists in the session, the analyzer explains that the result may reflect a framework limitation rather than agent drift.

Saved reports land in `~/.sentience/reports/undeclared-intent-<sid-prefix>-<timestamp>.md`. See the [user guide §10](https://github.com/crescerelabs/sentience-governor/blob/main/docs/guide/sentience_governor.md#10-analyzers--derived-metrics-over-captured-traces) for the full output schema.

### `sentience analyze policy-violations [target] [flags]`

Per-rule attribution of compute on turns where any of POL-001 through POL-005 fired. Available since v0.2.6.

```bash
sentience analyze policy-violations --latest                   # most recent session
sentience analyze policy-violations 7f3b                       # session-id prefix
sentience analyze policy-violations /path/to/session.jsonl     # explicit file
sentience analyze policy-violations --showcase                 # bundled clean example
sentience analyze policy-violations --latest --json            # structured output
sentience analyze policy-violations --latest --save            # write report, no prompt
sentience analyze policy-violations --latest --no-prompt       # no save prompt
```

Flag set is identical to `sentience analyze undeclared-intent`. Status values: `ok` (rules fired with token data), `no_violations` (clean session — also save-eligible), `no_token_data`, `no_turns`, `partial`. The metric uses **association language only** ("appeared on turns representing N tokens") — never savings or causality wording. It is a deterministic prioritization signal for operator inspection, not a savings estimate.

Saved reports land in `~/.sentience/reports/policy-violations-<sid-prefix>-<timestamp>.md`. For most operator use the [`sentience pulse`](#sentience-pulse-target-flags) wrapper is the preferred surface — pulse composes this analyzer with undeclared-intent and the advisory-flag summary into a single report.

### `sentience pulse [target] [flags]`

One-command session report. Composes the undeclared-intent analyzer (v0.2.4), the policy-violation burn-rate analyzer (v0.2.6), and an advisory-flag-occurrence summary into a single shareable Markdown report. Top-level command (not under `analyze`). Available since v0.2.6.

```bash
sentience pulse --latest                   # most recent session
sentience pulse 7f3b                       # session-id prefix
sentience pulse /path/to/session.jsonl     # explicit file
sentience pulse --showcase                 # bundled clean-session example
sentience pulse --latest --json            # structured output
sentience pulse --latest --save            # write report, no prompt
sentience pulse --latest --no-prompt       # no save prompt (footer still shown)
```

Each section ends with a one-line "Why it matters" translation — the metric becomes operator action, not raw data. Status values: `ok`, `partial`, `limited` (some analyzers had data, others didn't), `no_signal` (no analyzer produced usable signal). **All pulse statuses are save-eligible** — a `no_signal` pulse is itself a useful artifact. Since v0.2.8.2 the pulse also breaks total compute into the four token classes (cached read / cached write / prompt / completion, which reconcile to the total), and on an empty latest session it **shows** your most recent session that *does* have token data, with a transparent header naming it (an explicit `sentience pulse <id>` is honoured exactly).

Since v0.2.9 the pulse also surfaces **tool calls** as a first-class field (total, the four operation classes execute / read / write / delete, and the top tools by call count) and **measured tool-token attribution**: the tokens on turns that fired a tool call, plus a per-tool full-turn-credit view. Attribution stops at the turn (the model meters tokens per turn, not per tool), so figures read "tokens on turns involving tool X," never per-tool spend. The per-tool view is non-additive (a turn involving several tools credits each the full turn total).

Saved reports land in `~/.sentience/reports/pulse-<sid-prefix>-<timestamp>.md`. The Markdown footer includes a one-line email-list sign-up prompt. Set `SENTIENCE_NO_SYNC_PROMPT=1` to suppress the footer globally. See the [user guide §12](https://github.com/crescerelabs/sentience-governor/blob/main/docs/guide/sentience_governor.md#12-sentience-pulse) for the full output schema and a walkthrough of the three pre-rendered showcase scenarios.

### `sentience explain [--json]`

Explains **how Sentience counts**: the methodology behind the numbers, not a session analysis. Available since v0.2.9.

```bash
sentience explain          # human-readable methodology
sentience explain --json   # the same methodology as structured JSON
```

It states the four token classes, the dedupe-by-`llm_turn_id` rule, the per-turn (not per-tool) attribution boundary, the operation-type enum, and the join-key semantics. This is the authoritative answer to "what does this number mean?". It is also why a per-tool token figure is never reported (the model meters tokens per turn, not per tool). The `--json` output is deterministic and is what the MCP adapter will consume.

### `sentience demo {undeclared-intent|closed-loop}`

Runs a packaged demo session through the analyzer. Works from any install — no extra files, no Python-path knowledge. Available since v0.2.5.5.

```bash
sentience demo undeclared-intent   # a session that drifts off declared intent (~20.7% undeclared)
sentience demo closed-loop         # a clean session (100% declared) with profile findings
```

Useful for seeing what a populated analysis looks like before you've wired token attribution on your own sessions.

### `sentience profile {init|view|validate|export|import|edit}`

Manage the governance profile at `~/.sentience/profile.yaml`. Six verbs. Available since v0.2.5.

```bash
sentience profile init       # create a starter profile (inline-commented since v0.2.5.5)
sentience profile view       # print the active profile
sentience profile validate   # schema check (read-only — never mutates the file)
sentience profile edit       # open ~/.sentience/profile.yaml in an editor
sentience profile export /path/to/dest.yaml   # write the active profile to a path
sentience profile import /path/to/source.yaml # validate + install at ~/.sentience/profile.yaml
```

`profile edit` resolves an editor through `$VISUAL` → `$EDITOR` → `nano`/`vim`/`vi` → (macOS) TextEdit, so it works even when `$EDITOR` is unset. After you edit a generated profile, `profile validate` reports a clear informational note that the header hash is stale (the runtime uses the recomputed hash) — not an error.

Verbs:

| Verb | Behavior |
| :-- | :-- |
| `init` | Create `~/.sentience/profile.yaml` with sensible defaults. Refuses to overwrite an existing file — edit the existing one or delete it first. |
| `view` | Print the active profile (the file if it exists, otherwise the defaults with a banner). Source path and 12-char fingerprint go to stderr; YAML body goes to stdout (pipeable). |
| `validate [path]` | Schema check. **Read-only by design** — never modifies the source file. Reports content-hash integrity against the file's header. Exits non-zero on schema errors. `--strict` errors on unknown keys instead of warning. |
| `export <path>` | Write the active profile to an explicit path with a fresh header (content hash + timestamp recomputed). |
| `import <path>` | Read a profile from an explicit path, validate it, install at `~/.sentience/profile.yaml`. Refuses to install if validation fails. |
| `edit` | Open `~/.sentience/profile.yaml` in `$EDITOR`. Errors if no file exists (run `init` first) or if `$EDITOR` is unset. |

**The profile shapes three things** that the runtime applies to every governed session (Claude Code hook, MCP wrapper, LangChain handler):

1. **When undeclared intent is surfaced** — `session_intent.demand_at` is one of `session_start`, `first_write`, or `never`.
2. **When the agent has crossed a task boundary** — `task_boundary.signals` is any subset of `dir_change`, `file_type_shift`, `read_to_write_transition`, `time_gap`. Fires `TASK_BOUNDARY_CROSSED` on the next `SCOPE_ASSERTED`.
3. **Which tools should be treated as high-consequence** — `high_consequence.tools` is a list of regex patterns matched against `<tool_id>:<target_system>`. Fires `HIGH_CONSEQUENCE_DETECTED` on match.

All signals are observational. Nothing is blocked, scoped, or modified. See the [user guide §11](https://github.com/crescerelabs/sentience-governor/blob/main/docs/guide/sentience_governor.md#11-governance-profiles) for the full schema and walkthrough, and `examples/showcase/v025-closed-loop/` for a complete runnable example.

---

## `sentience-cli`

Raw, full-fidelity viewer for library-integrated agent traces. Reads NDJSON from a file or stdin.

```bash
sentience-cli path/to/agent.jsonl   # read a trace file
my-agent | sentience-cli            # pipe from stdout-sink
sentience-cli trace.jsonl --no-colour  # monochrome
```

Use `sentience-cli` for tens of events with full detail. Use `sentience` for hundreds of events with curation.

---

## `sentience-claude-code-hook`

Invoked by Claude Code, not by you directly. Wire it up per the [Quickstart](./quickstart.md#recommended-claude-code-hook-fastest-way-to-see-value).

Running it by hand is harmless but does nothing useful.

---

## Next

- [Troubleshooting →](./troubleshooting.md)

---

Stuck? → [Troubleshooting](./troubleshooting.md)
