# Changelog

Public version history for `sentience-governor`. Follows [Semantic Versioning](https://semver.org/).

Breaking changes bump the minor version until 1.0. After 1.0, breaking changes bump the major.

---

## 0.3.0 — 2026-07-17

**Governance Claude can call.** An opt-in MCP server (`sentience-mcp-server`, `pip install "sentience-governor[mcp]"`) lets Claude call Sentience governance tools directly inside a session: `sentience_explain`, `sentience_profile_view`, the last-completed-session reads `sentience_pulse` / `sentience_intent` / `sentience_violations` (each names the session it read), the structural-only `sentience_session_status` (no live token claims: token analysis is unavailable until SessionEnd), and the one forward-looking write, `sentience_declare_intent(objective, scope)`. A declaration is recorded as a server-written, append-only `INTENT_DECLARED` event; subsequent matching activity then stops firing POL-001 at capture, while pre-declaration events keep theirs (non-retroactive). It is classified `intent_source = inferred` (agent-declared through MCP: content-untrusted, not integrator-vouched) and fails closed on any uncertain session binding. Register per project with `sentience init claude-code --mcp` (opt-in, stdio, local, no HTTP); see the flip with `sentience demo declare-intent`. Additive: no `schema_version` bump, no new event types, declaration-free capture byte-identical to v0.2.9.

---

## 0.2.9 — 2026-07-06

**Tool-call visibility + methodology.** This release makes agent tool activity legible: which tools ran, and which kinds of work rode the expensive turns: execution economics, not raw token counts. `sentience pulse` surfaces tool calls as a first-class field (total, the four operation classes, top tools by call count) and measured tool-token attribution: the tokens on turns that fired a tool call, plus a per-tool full-turn-credit view. Attribution stops at the turn (the model meters tokens per turn, not per tool), so figures read "tokens on turns involving tool X," never per-tool spend; the per-tool view is non-additive. New `sentience explain` states exactly how Sentience counts. The `sentience open` summary splits policy violations from advisory flags and self-labels its tool counts. Additive, with no `schema_version` bump and no new event types.

---

## 0.2.8.3 — 2026-06-23

**Local-first cleanup.** Sentience Governor is local-first — your governance runs entirely on your machine. This release removes an unused, experimental cloud-telemetry surface (the old `sentience-sync` upload CLI: `register` / `run` / `update-check`) from the supported product, so Governor stays simple and honest about that. The `sentience-sync` command now prints a local-first notice. Local evidence capture, pulse reports, slash commands, policy evaluation, and Claude Code capture are unchanged.

---

## 0.2.8.2 — 2026-06-16

**Reports that explain themselves.** Where v0.2.8 stopped the chat surface from guessing, v0.2.8.2 removes the reason it had to — the measured output now carries its own meaning. The headline: `sentience pulse` breaks total compute into the four token classes that make it up — cached read / cached write / prompt / completion — and they reconcile exactly to the total. On a real Claude Code session the cache reads usually dominate (often well over 90%), the single most telling fact about the run, previously buried in raw fields. The report also states how it counts ("per-turn usage is deduped by requestId") so a cache-read total isn't misread as cumulative.

```bash
pipx install --upgrade sentience-governor
sentience pulse            # now shows the four token classes
```

**The live pulse just works now.** Resuming a Claude Code conversation mints a new session id, so the *newest* session is often an empty live segment — which used to make `sentience pulse` show an empty report exactly when you first tried it. Now, when the latest session has no token data yet, the pulse **shows your most recent session that does**, with a transparent header naming it, instead of an empty screen. An explicit `sentience pulse <id>` is always honoured exactly; the `analyze` commands name the session in their empty state.

Around that, a sweep of trust-surface copy fixes so nothing the report says is truer than the trace beneath it:

- **`sentience status --json` always returns JSON** — even on empty states (no trace directory / no sessions yet), preserving the exit code.
- **READ operations are no longer described as blocked writes** — the simulated consequence now reads correctly for reads vs mutating operations.
- **`context_size_tokens` is documented as a per-snapshot context size, not model token usage** — the name invited the misread.
- **Reserved profile fields read as reserved** — the `prompt_template` comment no longer implies the runtime prompts for intent.
- **The pulse sync sign-up CTA** now points to `getsentience.ai/sentience-sync`.
- **Skills add no unmarked prose after verbatim output** — any explanation stays behind the `Interpretation (not Sentience output):` label.

Additive release in the v0.2.8 line: no `schema_version` bump, no new event types; existing v0.2.4–v0.2.8, MCP, and LangChain traces and tooling are unchanged.

## 0.2.8 — 2026-06-11

**Claude Code slash commands + a hardened trust surface.** Sentience now lives inside the Claude Code chat. `sentience init claude-code` installs six operator-invoked slash commands — `/sentience-help`, `/sentience-pulse`, `/sentience-status`, `/sentience-profile`, `/sentience-violations`, `/sentience-intent` — so you can read governance signals without leaving the conversation. Built on v0.2.6.1's per-turn token capture, which is what makes the answers real on a Claude Code session. Pre-release clean-room testing surfaced a trust-boundary flaw in the flagship interaction — on a no-signal result, the chat surface could blur deterministic Sentience output with model interpretation — and it was fixed before publication.

```bash
pipx install sentience-governor
sentience init claude-code
# then restart Claude Code and type:
/sentience-help
/sentience-pulse
```

Each command is a thin wrapper over the CLI you already have: the CLI produces the deterministic numbers at preprocessing time, and Claude renders them inline and is instructed to show them verbatim — anything Claude adds beyond the rendered report is interpretation, not Sentience measurement. Use `--no-skills` to wire hooks without the commands, `--project` to install them into a project's `.claude/skills/` for a team, or `--force` to overwrite a hand-edited skill (a per-root manifest keeps installs idempotent and preserves your hand-edits by default).

Also in this release:

- **`sentience --version`** — prints the installed version and exits.
- **Trust-surface guardrails in every skill** — render verbatim; show no-signal results as-is; never put a Sentience report heading on the model's own words; label any requested explanation `Interpretation (not Sentience output):`. Measured first; interpretation second; abstention is never replaced with inference.
- **Empty states name the real cause** — per-turn token data is written when the session *ends*; a still-running session is the common case, not an unwired hook.
- **Counts you can reconcile** — `sentience status`/`list` split policy violations from advisory flags, and `status --json` exposes the full raw-vs-displayed reconciliation.
- **Measured rule counts before session end** — a turn-less session reports which rules fired, with token attribution explicitly pending.

The slash surface is operator-invoked only (`disable-model-invocation: true`), latest-captured-session only, and read-only. Backward-compatible additive release; existing traces, hooks, and tooling are unchanged.

---

## 0.2.6.1 — 2026-06-04

**Claude Code per-turn token capture.** A fast-follow that gives `sentience pulse` real token-burn attribution on Claude Code sessions. Before this, pulse reported `no_signal` there — the hook captured tool calls but had no per-turn token-burn attribution, so there was nothing to connect back to policy violations or undeclared intent.

Now the hook reads the main Claude Code session transcript at the end of the run and records token burn for each model turn, including the cached context tokens that dominate Claude Code usage. A turn can show 2 input tokens but read ~39,000 cached tokens.

```bash
sentience init claude-code        # now also wires the SessionEnd hook
sentience pulse --latest          # real per-turn token burn, not no_signal
```

Tool calls are matched to the model turn that issued them by Claude Code's tool-use id, not by event position, so a policy violation lands on the right turn. Reports separate total session token burn from the part attributable to a tool-call violation, and disclose subagent burn as excluded when Task/Agent activity is present. Token burn means token / context footprint — never dollar cost. No schema change; existing traces and tooling are unchanged. Capture is fail-open and idempotent.

**Notes**

- Token burn means token / context footprint, never dollar cost. No rate card, no estimated spend.
- Per-turn token usage is measured at the model-turn level; the report does not fabricate a per-tool token split when a turn issued several tool calls.
- Token-burn ranking is correct for the Anthropic / Claude Code path targeted by this release; convention-aware handling of OpenAI-style cache accounting is tracked as a follow-up cross-runtime refinement.

---

## 0.2.6 — 2026-06-03

**Sentience Pulse.** A single command summarizes a session across every analyzer we ship — undeclared-intent spend, policy-violation burn rate, and an advisory-flag summary — with a one-line "why it matters" translation under each section. Designed so an operator (or a teammate, advisor, CISO, CTO, or budget owner reading a saved report cold) can understand what happened in a session in under 60 seconds.

```bash
sentience pulse --latest         # one-command session report
sentience pulse --latest --save  # save the Markdown report
sentience pulse --showcase       # bundled example, works on any install
```

The policy-violation burn-rate analyzer is also exposed standalone as `sentience analyze policy-violations` for per-metric drill-down. Both surfaces use association language ("appeared on turns representing N tokens") rather than savings or causality wording — the metric is a deterministic prioritization signal for operator inspection, not a savings estimate.

### Added

- **`sentience pulse`** — top-level CLI command (NOT under `analyze`). Same flag set as the analyze subcommands: positional `target`, `--latest`, `--showcase`, `--json`, `--save`, `--no-prompt`. All pulse statuses are save-eligible (deliberate divergence from the standalone analyzer skip-save-on-non-`ok` contract — a `no_signal` pulse is itself a useful artifact).
- **`sentience analyze policy-violations`** — standalone burn-rate analyzer. Per-rule attribution of compute across POL-001 through POL-005. Status branches: `ok`, `no_violations`, `no_token_data`, `no_turns`, `partial`. Save-eligible statuses: `ok` and `no_violations`.
- **Three pre-rendered showcase pulses** under `examples/showcase/v026-pulse/` (`clean/`, `missing_intent/`, `mixed_violations/`) plus a `pulse_output.md` cross-link in the v0.2.5 closed-loop showcase. Byte-stable; regenerator at `examples/v026_pulse_demo.py`.
- **Sync-registration footer** in pulse Markdown reports. Eligibility decided from `~/.sentience/sync-state.json` and the `SENTIENCE_NO_SYNC_PROMPT` env var (set `SENTIENCE_NO_SYNC_PROMPT=1` to suppress the footer globally).

### Changed

- Renderer "Why it matters" line for the undeclared-intent section now branches on the actual undeclared / total distribution. Standalone `sentience analyze undeclared-intent` output is unchanged for the four primary status paths; the change affects the rendered copy that shows up inside pulse.

### What this release is NOT

- Not enforcement. Pulse reports drift; it does not block or modify agent behavior.
- Not a dashboard. Pulse is per-session, single-screen consumption.
- Not a savings estimate. Burn-rate copy never claims causality or quantifies a reclaim number.
- Not a schema bump. Same `schema_version: 1`. Existing v0.2.4 and v0.2.5 traces produce byte-identical analyzer output under v0.2.6.

---

## 0.2.5.5 — 2026-05-22

Fresh-install adoption patch. The first operator journey now works end to end without a walkthrough: install → discover commands → wire the Claude Code hook → capture a session → inspect and analyze → author and tune the governance profile. No schema changes; additive CLI surface only. Existing `~/.sentience/profile.yaml` files and existing traces work unchanged.

```bash
pip install --upgrade sentience-governor   # if pip-in-venv
pipx upgrade sentience-governor             # if pipx-installed
```

### Added

- **`sentience init claude-code [path]`** — one command wires the Claude Code hook into a project's `.claude/settings.json`. Idempotent (never clobbers existing hooks or settings; re-running is a no-op), and it resolves the correct hook-binary path for your install (pipx, pip-in-venv, or source).
- **`sentience demo undeclared-intent`** and **`sentience demo closed-loop`** — packaged, runnable demo sessions that work from any install, with no extra files or Python-path knowledge needed.
- **`sentience analyze undeclared-intent --showcase`** — analyze a bundled example trace, so you can see a populated analysis on a fresh install before token attribution is wired.
- **Inline-commented profiles.** `sentience profile init` now writes a `profile.yaml` with an explanatory comment above every field, so the file is readable on its own.

### Changed

- **`sentience` with no arguments** now prints a short command guide and exits cleanly, instead of an argument-usage error.
- **`sentience open` accepts a trace file path**, not just a session id or prefix — matching `sentience analyze`.
- **`--latest` (and session listing) order by session start time** rather than file modification time, so an actively-running session no longer reorders results between commands; `list`, `open --latest`, and `analyze --latest` always agree on which session is latest.
- **`sentience profile edit`** resolves an editor through `$VISUAL` → `$EDITOR` → `nano`/`vim`/`vi` → (macOS) TextEdit, instead of requiring `$EDITOR` to be set.
- **`sentience profile validate`** reports an edited profile with a clear informational note ("the header hash is stale; the runtime uses the recomputed hash") instead of an alarming "MISMATCH."
- Tool calls with no meaningful target (e.g. `ToolSearch`) render as a clean label instead of a placeholder `→ ???`.
- The `no_token_data` analyzer message now points you to `sentience analyze undeclared-intent --showcase` for a populated example.

### What this release is NOT

- **Not a feature release.** It is an adoption / quality-of-life patch — the analyzer, profile schema, and event model are unchanged from v0.2.5.
- **Not a schema bump.** Same six events, same advisory flags, same profile schema.
- **Not backward-incompatible.** Existing profiles and traces work without changes.

### Notes

- Per-turn token attribution for live Claude Code sessions is not in this release. Claude Code traces still report `no_token_data` for the undeclared-intent analyzer; capturing real per-turn tokens from Claude Code is planned for a later release. Use `--showcase` or `sentience demo` to see a populated analysis today.
- Versioning: 0.2.5.2 through 0.2.5.4 were internal pre-release validation builds and were never published. 0.2.5.5 is the first published build of this patch.

---

## 0.2.5.1 — 2026-05-18

First-impression copy patch. The CLI first-run welcome flow and post-install banner used legacy "hosted dashboard" framing left over from the v0.2.3 cycle; this release brings the CLI-side messaging into line with the strategic positioning (open-tier wrapper + enterprise control plane). No behavior changes, no schema changes, no API surface changes.

If you installed v0.2.5 and never ran `sentience` for the first time, the patch is invisible. If you ran the first-run flow, the welcome message now reads:

> *We're building an enterprise control plane on top of this open-tier wrapper. Drop an email if you'd like to hear when it ships. Hit Enter to skip.*

instead of the prior "hosted dashboard, shipping later this year" text.

```bash
pip install --upgrade sentience-governor   # if pip-in-venv
pipx upgrade sentience-governor             # if pipx-installed
```

### Changed

- `sentience_governor.cli.first_run` — three string replacements in the welcome block, the non-TTY install banner, and the post-subscribe success message. "Hosted dashboard" → "enterprise control plane." Removed the unforced "shipping later this year" timeline commitment.

### What this release is NOT

- **Not a feature release.** Strictly a copy patch for first-impression alignment.
- **Not a schema bump.** Same six events, same advisory flags, same profile schema as v0.2.5.
- **Not backward-incompatible.** Existing `~/.sentience/profile.yaml` files work without changes. Existing traces work without changes. 584 tests pass.

---

## 0.2.5 — 2026-05-13

v0.2.5 introduces **operator-defined governance posture** as the first durable, operator-authored representation of what Sentience expects of an agent. Until now, governance behavior was implicit — encoded in the default policy set and in whatever an integration passed at session start. With v0.2.5, every operator can author one **governance profile** at `~/.sentience/profile.yaml`, and every governed session — Claude Code hook, MCP wrapper, LangChain handler — picks it up automatically.

One profile, three wrapper surfaces. Authored once; travels with the operator. Encodes three things the runtime asks of every governed session: when undeclared intent is surfaced, when the agent has crossed a task boundary, which tools should be treated as high-consequence. All signals are observational; nothing is blocked. Sessions that run without a profile produce traces byte-identical to v0.2.4 — strictly additive.

```bash
pipx upgrade sentience-governor   # if pipx-installed
pip install --upgrade sentience-governor   # if pip-in-venv
sentience profile init             # create your first profile
```

### Added

- **Governance profiles at `~/.sentience/profile.yaml`.** A small, schema-versioned YAML file with three sections (`session_intent`, `task_boundary`, `high_consequence`) plus reserved slots for future composition features (`extends`, `policies`, `custom_rules`). The file is local, portable, and never uploaded anywhere. Sessions that run without one behave exactly as v0.2.4.
- **`sentience profile` CLI subcommand group.** Six verbs: `init` (create starter), `view` (inspect active profile), `validate` (schema check, **read-only**), `export` / `import` (move profiles between locations), `edit` (open in `$EDITOR`). The `validate` verb is read-only by design — it never modifies the operator-authored file, even when integrity checks fail.
- **Two new advisory flags that fire on `SCOPE_ASSERTED` events under a profile.** `TASK_BOUNDARY_CROSSED` fires when any configured boundary signal triggers (directory change, file-type shift, read-to-write transition, time gap). `HIGH_CONSEQUENCE_DETECTED` fires when a `<tool_id>:<target_system>` composite matches an operator-authored regex pattern. Both flags are forward-compatible — analyzers that don't know the values treat them as unknown strings and ignore them.
- **Profile fingerprint on every event.** The 12-character hex prefix of the profile's content hash. Operator can correlate any trace back to the profile that produced it. The field is omitted entirely on traces from sessions without a profile, so v0.2.4-shaped traces stay byte-identical under v0.2.5.
- **Profile-aware analyzer report sections.** `sentience analyze undeclared-intent` gains three optional sections — Profile (fingerprint + schema version), High-consequence operations, Task boundaries crossed. Each section is omitted when its underlying field is empty, so v0.2.4-shaped traces produce byte-identical analyzer output.
- **Closed-loop showcase.** A complete runnable embodiment of the loop — profile, agent recipe, generated trace, generated analyzer report, walkthrough — at `examples/showcase/v025-closed-loop/`. Companion script `examples/v025_closed_loop_demo.py` regenerates the trace and report deterministically.
- **Userdocs §11 "Governance Profiles."** Full user-guide section covering what a profile is, how to create one, the schema, the CLI verbs, what firing looks like in the trace, and integration notes for LangChain and MCP. See [`userdocs/sentience_governor.md` §11](https://github.com/crescerelabs/sentience-governor/blob/main/userdocs/sentience_governor.md#11-governance-profiles).

### Changed

- v0.2.4 §11 "Sinks" → §12. §12 → §13, §13 → §14, §14 → §15. Userdocs Contents block updated. Doc-internal anchors otherwise unchanged.
- Optional new dependency: `PyYAML>=6.0` (the profile loader needs YAML parsing).

### What this release is NOT

- **Not enforcement.** Profile signals appear in the trace and in analyzer output. Nothing is blocked, scoped, or modified. The schema reserves `prompt` / `block` / `deny` as future `on_match` values for the paid tier; in v0.2.5 they warn and fall back to `flag`.
- **Not a schema-version bump.** `schema_version` stays at 1. The reserved sections (`extends`, `policies`, `custom_rules`) ship recognized but ignored, so downstream features land additively without operators re-editing their profile.
- **Not cloud-required.** Profile lifecycle (author, validate, edit, export, import) runs entirely locally. No account, no API key, no network calls. `~/.sentience/profile.yaml` never leaves the machine; `sentience-sync` data flows are unchanged.
- **Not profile inheritance yet.** The `extends` field is recognized and preserved in `validate()` output, but the runtime does not yet resolve inheritance chains. Reserved for a future release.

See the [user guide §11](https://github.com/crescerelabs/sentience-governor/blob/main/userdocs/sentience_governor.md#11-governance-profiles) for the full walkthrough, schema reference, CLI commands, and the closed-loop example.

---

## 0.2.4 — 2026-05-08

v0.2.4 is the first release that derives **operational meaning** from execution-boundary traces rather than only recording them — the transition from instrumentation substrate to semantic analysis layer.

The first such derived metric: **undeclared-intent token spend**. Tells you how much compute was attributed to reasoning turns that touched execution outside the session's declared operational intent. Deterministic analyzer with replay-stable output. No schema changes, no breaking changes.

In some current coding-agent environments (Claude Code today, for instance), high undeclared-intent ratios may reflect framework limitations around intent declaration rather than actual agent drift. v0.2.4 makes that distinction visible.

```bash
pipx upgrade sentience-governor   # if pipx-installed
pip install --upgrade sentience-governor   # if pip-in-venv
```

### Added

- **`sentience analyze undeclared-intent` CLI subcommand.** New analyzer subcommand group. Reads any v0.2.3+ session trace and prints a one-screen-friendly headline + per-turn breakdown. Flags: positional target (session-id prefix OR file path OR omitted), `--latest`, `--json` (structured output), `--save` (write Markdown report directly), `--no-prompt` (suppress the post-render save prompt).
- **`sentience_governor.analyze.undeclared_intent.compute_undeclared_intent_spend()`.** Deterministic analyzer module with replay-stable behavior. Designed for large traces and replay-stable analysis — verified against 10k-event sessions in under a second.
- **Saved Markdown report path.** `~/.sentience/reports/undeclared-intent-<sid-prefix>-<timestamp>.md`. Includes the headline metric, per-turn breakdown, the operational-interpretation paragraph, and a two-vector footer (direct reply path + launch-list link).
- **Framework-aware result framing.** When no intent declaration exists anywhere in the session, the analyzer's CLI output and saved report explain that the result may reflect a framework limitation (e.g. Claude Code hooks today, which don't yet expose an intent-declaration primitive) rather than actual agent drift.
- **Showcase examples.** Three pre-rendered scenarios under `examples/showcase/` (low-undeclared, high-undeclared, no-intent). Plus a runnable end-to-end demo at `examples/v024_undeclared_intent_demo.py`.

### Changed

- Userdocs gain a new **§10 "Analyzers — derived metrics over captured traces"**. Covers the CLI, the saved Markdown report, the JSON output schema, and the status values.
- `sentience` CLI help output now documents the `analyze` subcommand group.

### What this release is NOT

- **Not a schema bump.** No new event types, no new payload fields. The analyzer reads existing v0.2.3 trace fields.
- **Not enforcement.** v0.2.4 ships *visibility* — the metric exposes the gap. Intervention modes (review, constraint, confirmation, block) follow downstream.
- **Not a dashboard.** Single-session reports today; consolidated views across runs are downstream. See [getsentience.ai/launch-list](https://getsentience.ai/launch-list/) to be notified.
- **Not a PDF report integration.** The PDF generator at `examples/sentience_business_report.py` (v0.2.3 cycle) is unchanged; the v0.2.4 metric is not surfaced through it. Consolidated visualization lands in future hosted surfaces.

See [`userdocs/sentience_governor.md` §10](https://github.com/crescerelabs/sentience-governor/blob/main/userdocs/sentience_governor.md#10-analyzers--derived-metrics-over-captured-traces) for the full guide.

---

## 0.2.3 — 2026-05-07

Set out to add token tracking. Ended up adding **execution-cost attribution** — token spend attached directly to execution-boundary traces, with per-turn identity so multi-tool-call attribution stays mathematically correct. No breaking changes; existing 0.2.2 wrappers, traces, and downstream tooling continue to work unchanged.

```bash
pipx upgrade sentience-governor   # if pipx-installed
pip install --upgrade sentience-governor   # if pip-in-venv
```

### Added

- **Launch-list email capture.** The `sentience` CLI prompts once on first invocation for an email so we can let you know when the hosted console ships. Easy to skip; never re-asks. Also at [getsentience.ai/launch-list](https://getsentience.ai/launch-list/). Architecturally separate from `sentience-sync` — the launch list never receives trace data, telemetry counts, or tool calls.
- **Optional LLM-token tracking on `CONTEXT_SNAPSHOT` events.** Eight new optional fields on `ClassificationHint` and `ContextSnapshotPayload`: `llm_prompt_tokens`, `llm_completion_tokens`, `llm_cached_read_tokens`, `llm_cached_write_tokens`, `llm_reasoning_tokens`, `model_identifier`, `provider`, `llm_turn_id`. All optional; non-adopters see zero schema change. Provider-accurate raw values pass through unchanged (Anthropic excludes cache from input; OpenAI includes; Sentience does not reconcile).
- **Per-turn attribution identity (`llm_turn_id`).** When one LLM turn produces multiple tool calls, the same token usage attaches to every emitted event in that turn, all sharing one `llm_turn_id`. Without this identity, aggregation across multi-tool-call turns becomes mathematically wrong. Consumers MUST dedupe by `(session_id, llm_turn_id)` before summing canonical token fields.
- **`SentienceCallbackHandler.on_llm_start` and `on_llm_end`.** New callback methods that capture per-turn LLM token usage from LangChain responses and attach it (with the turn's `llm_turn_id`) to subsequent tool-call events. Trace immutability preserved: events emitted before `on_llm_end` arrives carry the turn id but no token fields, and are never mutated retroactively when usage data lands.
- **`SentienceMiddleware.awrap_step`.** New optional LangGraph hook that aggregates token usage across messages within a step. Existing `awrap_tool_call` is unchanged and remains backward-compatible.
- **Defensive token-extraction helper module** (`sentience_governor.wrapper.token_extraction`). Handles Anthropic, OpenAI, and LangChain response shapes. Merges across shapes so Anthropic-via-LangChain cache fields are preserved.
- Claude Code hook adapter probes the hook payload for `usage` / `token_usage` defensively. Anthropic does not currently expose this in the hook payload; fields stay `None` until they do.

### Changed

- `sentience status` reassurance line now reads *"Sentience is governing your Claude Code sessions locally."* (was *"capturing your Claude Code sessions"*). Better matches the product brand and makes the local-first privacy stance explicit.

### What this release is NOT

- Not a schema-bump release. No new event types.
- Not a cost-calculation feature. We record raw provider-reported tokens; cost math lives in dashboards / downstream tools.
- Not a token-budget enforcement feature.
- Not a dashboard. The hosted console and downstream analytics ship separately — see [getsentience.ai/launch-list](https://getsentience.ai/launch-list/) to be notified.

See [`userdocs/sentience_governor.md`](https://github.com/crescerelabs/sentience-governor/blob/main/userdocs/sentience_governor.md#token-tracking-optional-v023) "Token tracking (optional)" for the integration guide and the full aggregation contract.

---

## 0.2.2 — 2026-04-28

Patch release. No API changes. No command changes.

### Fixed

- **Default network timeout for `sentience-sync` raised from 15 seconds to 30 seconds.** First-call latency on the cloud sync endpoint can exceed 15 seconds on slower client networks, causing `sentience-sync run` to fail with a network-error message even when the request would have eventually succeeded. The new default comfortably covers typical first-call latency while still failing fast on a genuinely unreachable network. Operators can override via `SENTIENCE_SYNC_TIMEOUT_SECONDS` or the `timeout_seconds` config key.
- **Removed misleading `(placeholder)` suffix from `sentience-sync status` output.** The status command previously appended `(placeholder)` next to the production endpoint URL, suggesting configuration was incomplete when it wasn't. Output now shows the endpoint URL plainly.

### Changed

- **README and userdocs now recommend `pipx install sentience-governor` as the canonical install method.** Modern macOS and Linux Pythons enforce PEP 668 and refuse `pip install` outside a virtualenv; `pipx` is the standard fix. Library integration via `pip install sentience-governor` inside an active venv is still documented and supported. See the updated [install guide](./install.md) and [troubleshooting](./troubleshooting.md).

---

## 0.2.1 — 2026-04-26 *(TestPyPI only; superseded by 0.2.2)*

Patch release. Published to TestPyPI for staging validation; not promoted to PyPI. The fixes in 0.2.1 are rolled forward into 0.2.2 along with additional improvements; install 0.2.2 instead.

### Fixed

- Default network timeout for `sentience-sync` raised from 5 seconds to 15 seconds, removing the need for a `SENTIENCE_SYNC_TIMEOUT_SECONDS` workaround in most cases. Subsequently raised again to 30 seconds in 0.2.2.

### Notes

- macOS users running Python from python.org may see `SSL: CERTIFICATE_VERIFY_FAILED` on first network call. This is a known Python packaging quirk, not a Sentience issue. One-time fix: `/Applications/Python\ 3.13/Install\ Certificates.command` (adjust the version number to match your Python install). The [troubleshooting guide](./troubleshooting.md) covers this in detail.

---

## 0.2.0 — 2026-04-23 *(TestPyPI only; superseded by 0.2.2)*

First release with live cloud sync. Published to TestPyPI for staging validation; not promoted to PyPI. Subsequent staging cycles surfaced a CLI default-timeout issue (fixed in 0.2.1 and again in 0.2.2) and a placeholder-text cosmetic bug (fixed in 0.2.2). Install 0.2.2 instead.

### Action required

Run:
```bash
sentience-sync register --email ... --name ...
```

### Breaking

- `sentience-sync register` now requires `--email` and `--name`.
- State file format bumped from v1 to v2.
- v1 state files are auto-migrated on next save. No manual migration.

### Added

- Live production endpoint at `https://sync.getsentience.ai/v1`.
- Bearer-token auth on `/v1/sync`.
- `user_agent` field in register payloads.
- Duplicate-sync is a successful no-op (exits 0 with `Already uploaded for this window`).
- Targeted error messages for three failure paths:
  - "Not registered. Run `register` first."
  - "Registration incomplete or outdated. Run `register` again."
  - "Authorization failed. Run `register` again (installation ID preserved)."
- State-file `0600` permissions on POSIX.
- Optional `--organization` and `--role` flags on register.

### Changed

- Default endpoint is live, not a placeholder.
- `/v1/update-check` is now GET with query string (was POST with body).
- Payload shapes match server contract exactly. Dropped `language_binding` and `deployment_label`.

### Security

- `installation_secret` stored with owner-only permissions on POSIX.
- Bearer token in header, never body.
- Source IPs hashed server-side, never stored raw.

### Correctness invariant

- `register` never regenerates your `installation_id` once one exists locally.
- `--force` refreshes the secret but preserves the ID.

### Upgrade from 0.1.9

```bash
pip install --upgrade sentience-governor
sentience-sync register --email you@example.com --name "Your Name"
```

Scripts calling `register` without flags must be updated.

Per-session traces, `sentience` / `sentience-cli`, Claude Code hook, and library-integration paths are unchanged.

---

## 0.1.9 — 2026-04-17

First public tag. Open-tier Sentience Governor runtime — execution-boundary instrumentation, policy evaluation, local trace generation, CLI tooling.

### Included

- Runtime with five control points plus `GOVERNANCE_ERROR`.
- MCP wrapper (`wrap_mcp_client` + `SentienceMCPAdapter`).
- LangChain adapter (`SentienceCallbackHandler`, `SentienceMiddleware`).
- Honest intent classification — inferred intents marked `inferred_low`.
- Five default policy rules (POL-001–POL-005), eight advisory flags.
- Session manager: `IDLE → ACTIVE → CLOSING → CLOSED`.
- Three sinks: `StdoutSink`, `FileSink`, `HttpLocalSink` (fail-open).
- Curated Claude Code hook viewer (`sentience`).
- Raw trace viewer (`sentience-cli`).
- Sentience Sync CLI (`sentience-sync`) — opt-in aggregated counts.

### Design commitments (apply to every release)

- No automatic uploads.
- Aggregates only, never raw events.
- Opt-in geo reporting.
- Structural isolation from the runtime.
- Failed uploads do not advance local state.

---

Stuck? → [Troubleshooting](./troubleshooting.md)
