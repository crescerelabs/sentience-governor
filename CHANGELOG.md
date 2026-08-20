# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.3.0.2] — 2026-08-20

**Patch release.** Under LangGraph, one agent run was split across several
governance sessions, and every session after the first reported a policy
violation on tools the caller had declared. This release scopes governance
state to the root invocation, so one run produces one session and the
violation stops firing.

### Fixed
- **A declared tool no longer reports POL-001 under LangGraph.** LangGraph
  fires chain-level callbacks once per graph *and* once per node. The
  LangChain callback handler kept one session in instance attributes and
  replaced it on every `on_chain_start`, so a single `.invoke()` opened
  several sessions. Only the first received an intent baseline; the rest
  evaluated every tool call against a null baseline and raised POL-001 on
  tools that were in fact declared. Governance state is now keyed by root
  invocation: a session is created only for a root start, and a nested start
  records ancestry and nothing else.
- **Governance survives a nested chain end.** Nested `on_chain_end` callbacks
  arrive before the outer one and used to tear the session down while the
  graph was still running, leaving every later tool call ungoverned. Teardown
  is now keyed on the ending run and only ends that run.
- **Tool events attribute to the correct run.** Tool callbacks are parented to
  the *node*, not to the root, so a single-hop parent check resolved to the
  wrong place. Attribution now walks the parent chain to the owning root, and
  an event that resolves to no root is skipped rather than attached to an
  arbitrary session.
- **Token usage, model, provider and `llm_turn_id` no longer cross between
  concurrent runs or between parallel branches of one graph.** Per-turn
  telemetry was held in single instance slots. Two parallel nodes of one graph
  have genuinely overlapping LLM turns, so a later-finishing branch could
  inherit an earlier branch's turn id while carrying its own usage and model —
  a record that is internally consistent-looking but wrong. Turn telemetry is
  now scoped per branch.
- **A second run for the same agent no longer closes the first.** The session
  registry allowed one active session per `agent_id` and force-closed the
  previous one, which ended a session that was still running. The registry now
  tracks a set of live sessions per agent, and closing one leaves the others
  active.

### Changed
- **`SessionManager.session_start` takes `allow_concurrent`, defaulting to
  `False`.** The default preserves existing behaviour exactly, including
  force-closing a prior session, which is how a single-agent-per-process
  runtime reclaims a session left open by a crashed run. Only the LangChain
  handler opts in.
- **One callback handler may now be shared across overlapping runs.** All
  mutable execution state is keyed by root invocation, so concurrent runs on
  threads or on one event loop cannot exchange sessions or telemetry.
- **README: "Full docs" now points at the documentation in this repository**
  rather than the deployed site, which is a separate surface that may not stay
  in step with the source here.

### Unchanged
- Policy semantics, scope evaluation, the event schema and the cache schema
  are untouched. A genuinely undeclared tool still raises POL-001.
- Callers that drive the handler without callback run ids — including every
  pre-existing integration and test — behave exactly as before.

## [0.3.0.1] — 2026-08-04

**Patch release.** The MCP server was broken on every new install of the
`[mcp]` extra: the extra declared an unbounded `mcp>=1.0`, and MCP SDK 2.0.0
removed the module the server imports. This release bounds the dependency,
makes the failure legible when it does occur, and removes the long-sunset
`sentience-sync` stub.

### Fixed
- **MCP server works again on a fresh install.** The `[mcp]` extra declared an
  unbounded `mcp>=1.0`; MCP SDK 2.0.0 removed `mcp.server.fastmcp`, which the
  server imports, so every new install of the extra resolved a version that
  could not run the server. The dependency is now `mcp>=1.0,<2`.
- **An unsupported `mcp` version is no longer reported as a missing
  dependency.** The two states printed the same message, so a user with `mcp`
  2.x installed was told to install `mcp`, got "already satisfied", and had no
  signal about the real cause. They are now distinguished, and the message
  names the installed version and the supported range.
- **Install remediation is context-aware.** A pipx-managed installation is no
  longer told to run ambient `pip install`, which cannot reach its virtual
  environment.

### Removed
- **`sentience-sync` command removed.** The Sync cloud-telemetry CLI was sunset
  in v0.2.8.3 and had remained as a stub that printed a local-first notice.
  The stub and its console entry point are now gone: **invoking
  `sentience-sync` returns `command not found`.** Nothing else changes, and
  the separate "Sentience Sync" **email list** at
  `getsentience.ai/sentience-sync` is unaffected and still available.

### Added
- **`make fresh-resolve`, a standing release gate.** Builds a wheel and installs
  it into a genuinely fresh environment per extra, with no tester-supplied pins,
  then runs a feature smoke test for each and records every resolved version.
  v0.3.0 passed every existing gate while shipping a broken extra, because the
  gates ran in a developer environment whose `mcp` predated 2.0. This one
  resolves dependencies the way a new user's machine does.

### Changed
- **Package summary and description rewritten.** The PyPI summary said the
  product would "hold each agent to" its declared intent, which implies
  enforcement the open-source release does not perform. Both the summary and the
  README now describe what it does: records, evaluates, and surfaces violations.
- **Public documentation corrected.** Removed unsupported absolute claims about
  outbound network calls, persistence, and observing every tool call; two
  product limitations now appear wherever the product is described in full,
  namely that declared intent is untrusted input and that Sentience governs
  supported agent actions rather than model behavior or content safety.
- **Install guidance now names the right command for the environment.** The
  `[mcp]` extra instructions distinguish a virtualenv from a pipx-managed
  install in the docs as well as the CLI.

## [0.3.0] — 2026-07-16

**Governance Claude can call.** This release adds an opt-in MCP server so
Claude can call Sentience governance tools directly inside a session: read
how the numbers are counted, read the last completed session's pulse /
intent / violations, read the current session's structural status, and
declare its intent for the session. It is opt-in only, stdio, and local
(no HTTP, no auth).

### Added
- **Sentience MCP server (`sentience-mcp-server`).** A stdio MCP server
  exposing seven tools. Session-independent reads: `sentience_explain`
  (the v0.2.9 methodology) and `sentience_profile_view` (your declared
  governance posture). Last-completed-session measured reads:
  `sentience_pulse`, `sentience_intent`, `sentience_violations` (each names
  the session it read, so a completed-session reading is never mistaken for
  the live one). Current-session `sentience_session_status`
  (structural-only: event count, tool-call counts by operation class, and
  policy / advisory counts so far; never a token, burn, or pulse figure,
  because token analysis is unavailable until SessionEnd). Installed via
  the optional extra: `pip install "sentience-governor[mcp]"`.
- **`sentience_declare_intent(objective, scope)`.** The one forward-looking
  write: the agent states, for the current session, the objective it is
  working toward and the operation targets that objective authorizes.
  Sentience records it as a server-written, append-only `INTENT_DECLARED`
  event; subsequent matching activity then stops firing POL-001 at capture
  (the flip from structural noise to signal), while pre-declaration events
  keep their POL-001 (non-retroactive). It is classified `intent_source =
  inferred`, `intent_confidence = inferred_low`: the declaration is
  agent-declared through the MCP channel, so its mechanism is reliable but
  its content is untrusted and NOT integrator-vouched. It fails closed on
  any uncertain session binding, and writes nothing.
- **`sentience init claude-code --mcp`.** Opt-in registration of the MCP
  server into a project's `.mcp.json`, plus a Sentience consent notice
  (read-only-mostly tool set, `declare_intent` is append-only, no
  policy/profile mutation tools, stdio-only/local, token analysis
  unavailable until SessionEnd). Never registered by default; the plain
  `sentience init claude-code` is unchanged.
- **`sentience demo declare-intent`.** A deterministic, self-contained
  showcase of the BEFORE/AFTER POL-001 flip a mid-session declaration
  produces (100% undeclared compute becomes 37.5% once the declaration
  lands, with the pre-declaration turn unchanged). It builds synthetic
  sessions through the real capture-time evaluator; it does NOT spawn the
  MCP server or exercise live session identification.

### Notes
- Additive: no `schema_version` bump and no new event types
  (`declare_intent` reuses `INTENT_DECLARED`). The core install is
  unchanged; the MCP SDK is an optional extra. Declaration-free capture is
  byte-identical to v0.2.9. One additive capture-side hook change
  (intent-baseline rehydration) lets a declaration suppress POL-001 for
  subsequent activity without any analyzer change.
- Session binding for `declare_intent` uses a tight 90-second freshness
  gate (a safety gate against misattributing a declaration to a prior
  session); structural reads use a looser tolerance. This reduces but does
  not fully eliminate a stale-env race; see the plan's section 7.1.


## [0.2.9] — 2026-07-06

**Tool-call visibility + methodology.** This release makes agent tool
activity legible: which tools ran, and which kinds of work rode the
expensive turns: execution economics, not raw token counts. It also adds
a command that states exactly how Sentience counts.

### Added
- **Tool calls in the pulse (F21).** `sentience pulse` surfaces tool-call
  counts as a first-class field: total, the four operation classes
  (execute / read / write / delete), and the top tools by call count, on
  both the CLI and Markdown surfaces.
- **Measured tool-token attribution (IR-3).** The pulse surfaces the
  tokens on turns that fired at least one tool call (headline) and a
  per-tool, full-turn-credit view. Attribution stops at the turn: the
  model meters tokens per turn, not per tool, so figures read "tokens on
  turns involving tool X," never per-tool spend. The per-tool view is
  non-additive (a turn involving several tools credits each the full turn
  total).
- **`sentience explain` (IR-5).** A new methodology command that states,
  deterministically, how Sentience counts: the token classes, the
  dedupe-by-`llm_turn_id` rule, the per-turn (not per-tool) attribution
  boundary, the operation-type enum, and the join-key semantics. `--json`
  emits the same methodology for machine consumers.

### Changed
- **`sentience open` summary (F19).** The session summary splits policy
  violations from advisory flags (matching `status` / `list`) instead of a
  single conflated "Violations" count, and self-labels the tool list as
  "Tool calls observed" / "Top tools by SCOPE_ASSERTED count" so it reads
  as tool-call frequency, not events.

### Notes
- Additive only, with no `schema_version` bump and no new event types. Every
  surface derives from events already captured since v0.2.6.1.


## [0.2.8.3] — 2026-06-23

**Local-first cleanup.** Sentience Governor is local-first: your
governance runs entirely on your machine. This release removes an unused
experimental cloud-telemetry surface from the supported product so
Governor stays simple and honest about that. Local evidence capture, pulse
reports, slash commands, policy evaluation, and Claude Code governance are
unchanged. Optional cloud / control-plane capabilities may return later as
part of the MCP roadmap.

### Removed

- **Sync cloud telemetry.** The experimental `sentience-sync` upload CLI
  (`register` / `run` / `update-check`, which sent aggregate rule-fire
  counts to a cloud endpoint) is removed from the supported product. The
  `sentience-sync` command now prints a local-first notice and exits 0;
  the telemetry implementation is gone. The runtime never imported it, so
  nothing local changes.

### Changed

- Docs reframed to describe the sunset; the `sentience-sync` command now
  prints a local-first notice instead of running.

### Notes

- Backward-compatible for all local features: pulse, status, profile,
  violations, intent, and Claude Code capture are unchanged. No
  `schema_version` bump, no new event types.

## [0.2.8.2] — 2026-06-16

**Reports that explain themselves.** Where v0.2.8 stopped the chat
surface from guessing, v0.2.8.2 removes the reason it had to — the
measured output now carries its own meaning. The pulse surfaces the four
token classes that make up a session's compute (cache reads usually
dominate a Claude Code run — often well over 90%), states how per-turn
usage is counted, and — when the session you're in has no token data yet
— the live pulse now shows your most recent session that *does*, instead
of an empty screen. Around that:
a sweep of trust-surface copy fixes so nothing the report says is truer
than the trace beneath it. Backward-compatible additive release: no
`schema_version` bump, no new event types. Existing v0.2.4–v0.2.8, MCP,
and LangChain traces and tooling are unchanged.

### Added

- **The four token classes, in the pulse.** `sentience pulse` now breaks
  total compute into cached read / cached write / prompt / completion —
  the single most telling fact about a Claude Code session (cache reads
  typically dominate), previously only reachable by summing raw fields.
  The four reconcile exactly to the total. Measured, not inferred;
  carried in `--json` too.
- **The report states its own methodology.** A one-line note —
  "Per-turn usage is deduped by requestId" — so cache-read totals are
  not misread as cumulative.
- **The live pulse just works.** When the latest session has no
  token-bearing turns (resuming a Claude Code conversation mints a new
  session id, so the newest is often an empty live segment), `sentience
  pulse` now **shows the most recent session that *does* have token
  data** — with a transparent header naming it — instead of an empty
  report. An explicit `sentience pulse <id>` is always honoured exactly.
  (The `analyze` commands name the session in their empty state.)
- **`sentience status --json` always returns JSON.** The empty-state
  early returns (no trace directory / no sessions captured) now emit a
  JSON object instead of human text, preserving the exit code — a
  `--json` contract fix.

### Changed / Fixed

- **READ operations are no longer described as blocked writes.** POL-001
  also fires on reads (a tool call outside declared intent); the
  simulated consequence now reads correctly for READ vs mutating
  operations instead of always saying "This WRITE operation would have
  been blocked."
- **`context_size_tokens` is documented as a per-snapshot context size,
  not model token usage** — the field name invited that misread.
- **Reserved profile fields are no longer described as live.** The
  `prompt_template` comment now reads "reserved; not read by the
  runtime" instead of implying the runtime prompts for intent.
- **The pulse sync sign-up CTA** now points to
  `getsentience.ai/sentience-sync`.
- **Skills add no unmarked prose after verbatim output.** The shell-out
  skills were tightened so any explanation stays behind the
  `Interpretation (not Sentience output):` label — the v0.2.8 trust
  contract, enforced more tightly.
- **Internal version reference removed** from the bundled help skill.

### Notes

- Additive release in the v0.2.8 line. No `schema_version` bump, no new
  event types; the capture pipeline is unchanged.

## [0.2.8] — 2026-06-11

**Claude Code slash commands + a hardened trust surface.** Brings
Sentience into the Claude Code chat: `sentience init claude-code` now
installs six operator-invoked slash commands, so governance signals are
one keystroke away — no terminal context-switch. Built on v0.2.6.1's
per-turn token capture, which is what makes the answers real on a
Claude Code session. Pre-release clean-room testing surfaced a
trust-boundary flaw in the flagship interaction — on a no-signal
result, the chat surface could blur deterministic Sentience output with
model interpretation — and it was fixed before publication.
Backward-compatible additive release: no `schema_version` bump, no new
event types. Existing v0.2.4–v0.2.6.1, MCP, and LangChain traces and
tooling are unchanged.

### Added

- **Six bundled Claude Code skills**, installed by `sentience init
  claude-code`: `/sentience-help`, `/sentience-pulse`,
  `/sentience-status`, `/sentience-profile`, `/sentience-violations`,
  `/sentience-intent`. Five shell out to the existing read-only CLI
  (`pulse`, `status`, `profile view`, `analyze policy-violations`,
  `analyze undeclared-intent`); `/sentience-help` is static onboarding.
  The CLI produces the deterministic numbers at skill-preprocessing
  time; Claude renders them inline and is instructed to show them
  verbatim. Anything Claude adds beyond the rendered report is
  interpretation, not Sentience measurement.
- **The two-layer contract in every skill.** Each shellout skill
  instructs Claude: render the CLI output verbatim; show
  no_signal/no_turns/no_token_data as-is and stop; never use a
  Sentience report heading on its own output; interpretation only on
  explicit ask, opening with `Interpretation (not Sentience output):`.
  Measured first; interpretation second; abstention is never replaced
  with inference.
- **`--no-skills` / `--project` / `--force` flags** on `sentience init
  claude-code`. `--no-skills` wires hooks only; `--project` installs
  into a project's `.claude/skills/` (shareable via git) instead of the
  personal `~/.claude/skills/`; `--force` overwrites a hand-edited skill.
- **Idempotent skill install** via a per-root sidecar manifest
  (`.sentience-skills.json`): a new release cleanly updates managed
  skills, while a skill you've hand-edited is preserved unless `--force`.
- **`sentience --version`.** The `sentience` CLI now prints its
  installed version and exits — a standard affordance it was missing.
- **Installer detection.** `init claude-code` prints restart-vs-no-
  restart guidance based on whether the skills directory existed before
  the run, and probes `sentience` PATH-resolvability after install
  (warns, never fails — the skills shell out to the local binary).
- **`sentience status --json`** — machine-readable count
  reconciliation: policy violations vs advisory flags vs
  baseline-filtered codes vs raw total.
- **Measured rule counts on turn-less sessions.** `analyze
  policy-violations` (and its report) now lists which rules fired —
  with token attribution explicitly pending session end — instead of
  reporting nothing (`unpaired_by_rule`, additive field).

### Changed

- **Empty-state messages name the real cause.** All no-signal analyzer
  surfaces now lead with: per-turn token data is written when the
  Claude Code session *ends*; the session may still be running. (The
  old copy suggested re-wiring hooks first, which misdiagnosed the
  common live-session case.)
- **`sentience status` / `list` split the counts.** "Policy
  violations" and "Advisory flags" are now separate lines (list shows
  `⚠ Nv/Ma`) — never an advisory count under a "Violations" label.

### What this release is NOT

- **Not Claude-initiated.** Claude does not auto-run these
  (`disable-model-invocation: true`) — they are operator-invoked only.
  Claude-initiated governance is a later MCP release.
- **Not cross-session.** No list / search / history / aggregate or
  session-id selection from the slash surface; latest captured session
  only. Cross-session belongs to a future control-plane tier.
- **Not a mutation surface.** `/sentience-profile` is view-only; no
  slash command mutates governance, session, or remote state.

### Notes

- A newly created `~/.claude/skills/` requires restarting Claude Code;
  if the directory already existed, the new commands appear within a
  few seconds.
- Slash commands themselves run at skill-preprocessing time and are not
  captured as governance events.

## [0.2.6.1] — 2026-06-04

**Claude Code per-turn token capture.** Fast-follow to v0.2.6. Before
this release, `sentience pulse` rendered `no_signal` on a live Claude
Code session: the hook captured tool-call events but no per-turn
token-burn data, so burn-rate and undeclared-intent had nothing to
attribute. v0.2.6.1 closes that gap — "wire Claude Code → see intent
drift + token burn" is now true on Claude Code, bringing it in line with
the token-aware MCP / LangChain paths. Backward-compatible additive
release: no `schema_version`
bump, no new event types, no cost/rate-card subsystem. Existing v0.2.4
/ v0.2.5 / v0.2.6 / MCP / LangChain traces and tooling are unchanged.

### Added

- **SessionEnd token capture (Claude Code).** The hook now parses the
  session transcript at `SessionEnd` and appends one token-bearing
  `CONTEXT_SNAPSHOT` per model turn (`llm_turn_id` = the transcript
  `requestId`), carrying the turn's provider-native token categories
  (prompt, completion, cache-read, cache-write). The values are written
  into the **existing canonical `CONTEXT_SNAPSHOT` token fields** via the
  existing `extract_anthropic_usage` — no new schema. Cache tokens
  dominate Claude Code usage
  (a turn can show 2 input tokens but ~39,000 cached) — capturing them
  is what makes burn reporting honest. Fail-open and idempotent: a
  missing / locked / malformed transcript never breaks session end,
  and a repeated `SessionEnd` does not double-count.
- **`tool_use_id` join.** Live tool-call events (`SCOPE_ASSERTED`,
  `CONTEXT_SNAPSHOT`, `MEMORY_WRITE_ATTEMPT`) now carry the
  `tool_use_id` Claude Code assigns each call. The burn-rate and
  undeclared-intent analyzers attribute a policy violation to its model
  turn **by `tool_use_id`, not by event position** — so each tool's
  violation lands on the turn that actually issued it.
- **`sentience init claude-code` wires `SessionEnd`** alongside
  `PreToolUse` / `PostToolUse`. Idempotent: re-running on an existing
  v0.2.6 install adds only the `SessionEnd` hook and leaves Pre/Post
  untouched.
- **Total vs governance-attributable token burn.** Reports now
  distinguish total session token burn from the portion attributable to
  a tool-call violation, and disclose the remainder (reasoning / answer
  turns) as real burn — so "not attributed" is never read as "no burn."
- **Subagent-exclusion disclosure.** When Task/Agent activity is
  present, reports state that subagent token burn is excluded (subagent
  transcripts are out of scope this release) rather than silently
  presenting partial burn as full-session burn.

### Notes

- Token **burn** means token / context footprint, never dollar cost.
  No rate card, no estimated spend.
- Per-turn token usage is measured at the model-turn (`requestId`)
  level; the report does not fabricate a per-tool token split when a
  turn issued several tool calls.
- Token-burn ranking is correct for the Anthropic / Claude Code path
  targeted by this release; convention-aware handling of OpenAI-style
  cache accounting is tracked as a follow-up cross-runtime refinement.

## [0.2.6] — 2026-06-03

**Sentience Pulse.** v0.2.6 adds a top-level `sentience pulse`
command that composes the v0.2.4 undeclared-intent analyzer with a
new v0.2.6 policy-violation burn-rate analyzer plus an advisory-
flag-occurrence summary into a single shareable session report.
The burn-rate analyzer is also exposed standalone as `sentience
analyze policy-violations`. Backward-compatible additive release:
no schema bump (`schema_version` stays at 1), no new event types,
no new advisory-flag values. Existing v0.2.4 and v0.2.5 wrappers,
traces, and tooling continue to work unchanged.

### Added

- **`sentience pulse [target] [flags]`** — one-command session
  report. Top-level CLI command (NOT under `analyze`, by design —
  pulse is the adoption surface). Flags: positional `target`,
  `--latest`, `--showcase`, `--json`, `--save`, `--no-prompt`.
  Save path: `~/.sentience/reports/pulse-<sid-prefix>-<timestamp>.md`.
  All pulse statuses are save-eligible (deliberate divergence from
  the standalone analyzer skip-save-on-non-`ok` contract).
- **`sentience analyze policy-violations [target] [flags]`** —
  standalone burn-rate analyzer for per-metric drill-down. Same
  flag set as `sentience analyze undeclared-intent`. Save path:
  `~/.sentience/reports/policy-violations-<sid-prefix>-<timestamp>.md`.
  Save-eligible statuses: `ok` and `no_violations` (clean-session
  reports are intentionally shareable).
- **`compute_policy_violation_burn_rate(events)`** —
  pure-function analyzer in `sentience_governor.analyze.policy_violation_burn_rate`.
  Per-rule attribution of compute on turns where any of POL-001
  through POL-005 fired. Output dict carries `total_tokens`,
  `violation_associated_tokens` (deduped across turns),
  `violation_firing_turns`, `by_rule[RULE]` per-rule slot, plus
  non-additivity notes (`notes` for Markdown, `notes_short` for
  CLI). Five status branches: `ok`, `no_violations`,
  `no_token_data`, `no_turns`, `partial`.
- **`compute_pulse(events)`** — pure-function composition module
  in `sentience_governor.analyze.pulse`. Imports the two
  analyzers, summarizes advisory-flag occurrences, normalizes
  each sub-analyzer's status to one of five categories
  (`usable_ok` / `usable_clean` / `limited_signal` / `partial` /
  `no_signal`), and merges to one of four pulse-level statuses
  (`ok` / `limited` / `no_signal` / `partial`). Sync-prompt
  eligibility is NOT computed here (preserves analyzer purity);
  the CLI handler attaches it before rendering.
- **`render_pulse_cli(result, color=True)` and
  `render_pulse_markdown(result)`** — pure renderers in
  `sentience_governor.analyze.renderers`. CLI output fits within
  80 columns. Markdown output is designed to be shareable as a
  standalone artifact. Both render every section with a one-line
  "Why it matters" interpretive line.
- **Sync-registration footer.** Pulse Markdown footer surfaces a
  one-line sync-registration prompt for operators who haven't
  registered yet. Eligibility decided in the CP6 CLI handler from
  `~/.sentience/sync-state.json` and the `SENTIENCE_NO_SYNC_PROMPT`
  env var. `SENTIENCE_NO_SYNC_PROMPT=1` suppresses the footer
  globally (precedence over registration state).
- **Showcase examples.** `examples/showcase/v026-pulse/` ships
  three pre-rendered operator stories: `clean/` (no_violations),
  `missing_intent/` (Claude Code-style POL-001 on every mutating
  turn), `mixed_violations/` (POL-001 + POL-003 + POL-005 across
  multiple turns). `examples/v026_pulse_demo.py` is the byte-
  stable generator. The v0.2.5 closed-loop showcase has a
  `pulse_output.md` cross-link that complements the `clean/`
  sub-case.
- **Six new test files** covering analyzer + renderer + CLI +
  showcase byte-stability: `tests/test_policy_violation_burn_rate.py`
  (53 tests), `tests/test_renderers.py` (100 tests including
  CP5 pulse renderer coverage), `tests/test_analyze_policy_violations_cli.py`
  (27 tests), `tests/test_pulse.py` (60 tests),
  `tests/test_pulse_cli.py` (29 tests), `tests/test_v026_pulse_demo.py`
  (12 tests).

### Changed

- **Renderer copy fix** (`render_cli` / `render_markdown_report`
  for undeclared-intent within pulse). The undeclared-intent
  section's "Why it matters" line now branches on the actual
  undeclared / total distribution: surface-bound (no intent + 100%
  undeclared) gets the surface-bound framing; mixed sessions get
  the harder-to-attribute framing; clean sessions (`undeclared==0`)
  get the every-turn-attributable framing. Standalone `sentience
  analyze undeclared-intent` output is unchanged for `ok` /
  `partial` / `no_token_data` / `no_turns` paths; the renderer
  change affects shared helpers used by both surfaces.

### What this release is NOT

- **Not enforcement.** Same observational posture as v0.2.0–v0.2.5.
  Pulse reports drift; it does not block or modify agent behavior.
- **Not a dashboard.** Pulse is per-session, single-screen
  consumption. Cross-session aggregation is v0.3.x console
  territory.
- **Not a savings estimate.** Burn-rate copy uses association
  language only ("appeared on turns representing N tokens"). It
  never claims causality or quantifies a reclaim / savings number.
- **Not a schema bump.** Same `schema_version: 1`. Same six event
  types. Same `policy_violations` envelope field. No new payload
  fields. v0.2.4 / v0.2.5 traces produce byte-identical analyzer
  output under v0.2.6 (regression-pinned).

## [0.2.5.5] — 2026-05-22

**Patch release — fresh-install adoption.** Makes the first operator
journey work without a founder walkthrough: install → discover
commands → wire the Claude Code hook → capture a session →
inspect/analyze → view/edit the governance profile. Surfaced by a
validation pass against the pipx-installed artifact (not the dev
`.venv`), which exposed friction the dev path masked. No schema
changes; additive CLI surface only.

> Versioning note: 0.2.5.2 through 0.2.5.4 were internal pre-release
> validation builds (uploaded only to TestPyPI during testing) and
> were never published to PyPI. TestPyPI does not allow re-uploading a
> version, so each validation revision took the next patch number.
> 0.2.5.5 is the first published build of this patch — the exact
> artifact that passed validation.

### Added

- `sentience init claude-code [path]` — one command to wire the
  Claude Code hook into a project's `.claude/settings.json`.
  Idempotent merge (never clobbers existing hooks/settings); resolves
  the hook binary belonging to the *same install* as the running CLI
  (sibling of the interpreter, falling back to `$PATH`), across
  pipx/pip/source. (F-V1)
- `sentience demo undeclared-intent` / `sentience demo closed-loop` —
  packaged, runnable demos that work from any install (no Python-path
  knowledge needed). (F-V6)
- `sentience analyze undeclared-intent --showcase` — analyze a bundled
  closed-loop showcase trace, so a fresh install can see a populated
  analysis before token capture is wired. (F-V5, cheap half)
- Default profile YAML from `sentience profile init` now ships inline
  explanatory comments on every field — readable standalone by a
  non-developer. Content hash is unaffected (computed from data, not
  file text). (F-V7)

### Changed

- `sentience` with no arguments now prints a helpful command guide
  and exits 0, instead of an argparse error. (F-V2)
- `sentience open` now accepts a trace **file path** as well as a
  session id/prefix — consistent with `sentience analyze`. (F-V10)
- `--latest` (and session listing) now order by **session start time**
  rather than file mtime, so an actively-written live session no
  longer reorders results between commands; `list`, `open --latest`,
  and `analyze --latest` always agree. (F-V4)
- `sentience profile edit` falls back through `$VISUAL` → `$EDITOR` →
  `nano`/`vim`/`vi` → (macOS) TextEdit instead of hard-failing when
  `$EDITOR` is unset. (F-V8)
- `sentience profile validate` reports an edited profile with an
  informational note ("Header hash is stale … runtime uses the
  recomputed hash …") instead of the alarming `MISMATCH`. (F-V9)
- Trace formatter renders tools with no meaningful target as a clean
  bare label (e.g. `ToolSearch`) instead of the broken-looking
  `ToolSearch → ???`. (F-V3)
- `no_token_data` analyzer output now points the operator at a
  concrete next step (`sentience analyze undeclared-intent
  --showcase`). (F-V5)

### Notes

- Deep Claude Code SessionEnd token capture (so live sessions carry
  real per-turn token data) is **not** in this patch; it is scheduled
  for 0.2.6.
- 39 new tests; full suite green (623 passing).

## [0.2.5.1] — 2026-05-18

**Patch release — first-run copy alignment.** Removes legacy
"hosted dashboard" framing from the first-run welcome flow and the
post-install banner. Brings CLI-side messaging into line with the
strategic positioning (open-tier wrapper + enterprise control
plane). No behavior changes; no schema changes; no API surface
changes. Strictly a copy patch for first-impression alignment.

### Changed

- `sentience_governor.cli.first_run` — three string replacements
  in the welcome block, the non-TTY install banner, and the
  post-subscribe success message. "Hosted dashboard" → "enterprise
  control plane." Removed the unforced "shipping later this year"
  timeline commitment.

### Why a patch rather than rolling into 0.2.6

The 0.2.5 release is the first impression for operators who
pip-install after seeing the deck or the Substack. Mismatched
vocabulary on the CLI side (dashboard) vs. the strategy surfaces
(control plane) creates an avoidable credibility hit. Patching
now keeps the first-impression aligned without waiting on the
0.2.6 cycle.

## [0.2.5] — 2026-05-13

**Operator-defined governance posture.** v0.2.5 introduces the first
durable, operator-authored artifact the runtime evaluates against:
the **governance profile** at `~/.sentience/profile.yaml`. One
profile, evaluated across every wrapper surface (MCP, LangChain,
Claude Code hook). Authored once, travels with the operator. Encodes
three things the runtime asks of every governed session: when intent
must be declared, when the agent has crossed a task boundary, which
tools always surface. All signals are observational; no enforcement.
Backward-compatible: sessions without a profile produce traces
byte-identical to v0.2.4.

### Added

- **Governance profile module.** `sentience_governor.profile` package
  with `GovernanceProfile` loader. YAML schema (`schema_version: 1`)
  covering `session_intent`, `task_boundary`, and `high_consequence`
  sections plus three reserved sections (`extends`, `policies`,
  `custom_rules`) for future composition features. Read-only
  `validate()` returning `ProfileValidationResult` with per-field
  errors and warnings; never mutates the operator-authored file.
  Content hash + 12-char fingerprint computed deterministically from
  the canonical JSON form (whitespace, comment, and key-order
  invariant).
- **`sentience profile` CLI subcommand group.** Six verbs:
  `init`, `view`, `validate`, `export`, `import`, `edit`.
  `validate` is read-only by architectural decision (verified by
  regression-guarded mtime + bytes-identical test). `init` refuses
  to overwrite existing files. `edit` honors `$EDITOR` and errors
  cleanly when unset.
- **Runtime integration.** `SessionManager.session_start()` accepts
  an optional `profile` keyword argument; profile is stored
  immutably on `_SessionEntry` for the session's lifetime.
  `EventBuilder` applies three profile-driven transforms after
  base `_eval_scope`: POL-001 gating per `session_intent.demand_at`,
  task-boundary signal detection per `task_boundary.signals`, and
  high-consequence regex match per `high_consequence.tools`. Each
  transform is bounded: malformed regexes skip silently, signals
  with no prior state defer until baseline exists, missing fields
  use documented defaults.
- **Two new advisory flag values.** `TASK_BOUNDARY_CROSSED` fires
  on `SCOPE_ASSERTED` when any active `task_boundary.signals`
  triggers against the previous event's state.
  `HIGH_CONSEQUENCE_DETECTED` fires when the
  `<tool_id>:<target_system>` composite matches any regex in
  `high_consequence.tools`. Both are forward-compatible: analyzers
  that don't recognize the values treat them as unknown strings
  and ignore them.
- **Three new envelope/payload fields, all None-omitted.**
  `GovernanceEvent.profile_fingerprint` (12-char hex, every event in
  a profile-governed session). `AgentRegisteredPayload.profile_loaded`
  (boolean) and `profile_schema_version` (integer) on the
  `AGENT_REGISTERED` event only. All three fields use the v0.2.3
  Track-2 `@model_serializer` pattern — None values are omitted
  from serialized JSON entirely, so traces from sessions without a
  profile are byte-identical to v0.2.4.
- **Profile-aware analyzer.** `compute_undeclared_intent_spend`
  extracts the three metadata fields from `AGENT_REGISTERED` and
  collects `HIGH_CONSEQUENCE_DETECTED` + `TASK_BOUNDARY_CROSSED`
  advisory flag events into ordered lists. The analyzer never reads
  `~/.sentience/profile.yaml` directly — the trace is the
  authoritative source, preserving the v0.2.4 pure-function and
  byte-stable output guarantees.
- **Three new optional analyzer report sections** (CLI + Markdown):
  Profile (fingerprint + schema version), High-consequence
  operations (per-event table), Task boundaries crossed (per-event
  table). Each section omitted when its underlying field is absent
  or empty — v0.2.4 traces produce byte-identical analyzer output.
- **Wrapper wiring across all three surfaces.** New
  `GovernanceProfile.from_default_path_or_none()` classmethod;
  MCP wrapper (`wrap_mcp_client`), LangChain handler
  (`SentienceCallbackHandler.on_chain_start`), and Claude Code hook
  all load the profile transparently at session start. No keyword
  arguments changed; existing integration code continues to work.
- **Closed-loop showcase.** New `examples/showcase/v025-closed-loop/`
  directory containing `profile.yaml`, `CLAUDE.md` recipe,
  pinned `session.jsonl` (synthesized trace), pinned
  `analyzer_output.md`, and a walkthrough `README.md`. Companion
  runnable script: `examples/v025_closed_loop_demo.py`. Byte-stable:
  deterministic event IDs + fixed timestamp + deterministic
  fingerprint mean re-running produces identical outputs, verified
  by `tests/test_v025_closed_loop_demo.py`.
- **Userdocs §11 "Governance Profiles".** New top-level section
  covering what a profile is, how to create one, the YAML schema,
  the six CLI commands, what firing looks like in the trace, the
  closed-loop walkthrough, and integration notes for LangChain and
  MCP. §12–§15 renumbered from prior §11–§14.
- **PyYAML>=6.0 dependency.** The profile loader needs YAML parsing;
  PyYAML is the de-facto Python YAML library.

### Changed

- `pyproject.toml` version bump 0.2.4 → 0.2.5.

### What this release is NOT

- **Not enforcement.** Profile signals appear in the trace and in
  analyzer output. Nothing is blocked, scoped, or modified. The
  schema reserves `prompt` / `block` / `deny` as `on_match` values
  for future paid-tier behavior; in v0.2.5 they warn and fall back
  to `flag`.
- **Not a schema-version bump.** `schema_version` stays at 1. All
  additions are additive at the open-tier substrate; the reserved
  sections (`extends`, `policies`, `custom_rules`) ship recognized
  but ignored, so downstream releases land without operators
  re-editing their profile.
- **Not a sync change.** `sentience-sync` is unchanged.
  `~/.sentience/profile.yaml` lives alongside `sync-state.json` and
  `sync-config.json` but is independent of them. Profile data is
  never uploaded to Sentience Cloud (profiles describe operator
  posture; sync uploads aggregated rule-fire counts only — see
  `userdocs/sentience_sync.md` for the sync data contract).
- **Not cloud-required.** The entire profile lifecycle (author,
  validate, edit, export, import) runs locally. No account, no API
  key, no network calls.
- **Not profile inheritance.** The `extends` field is recognized
  by the loader and preserved in `validate()` output, but the
  runtime does not yet resolve inheritance chains. Reserved for
  a future release.

### Test count delta

- v0.2.4 baseline: 519 passing.
- v0.2.5 release: **584 passing** (+65 over v0.2.4).
- Golden-trace byte-stability tests preserved across all v0.2.5
  changes — operator-facing API and trace shape regression-guarded
  against v0.2.4 baseline.

## [0.2.4] — 2026-05-08

First **derived metric** over the v0.2.3 token-attribution substrate:
**undeclared-intent token spend**. For a given session, computes how
much of the agent's compute was attributed to reasoning turns that
touched execution outside the session's declared operational intent.
No schema changes. No new event types. No probabilistic inference.
Deterministic analyzer with replay-stable output, additive only.
Existing v0.2.3 integrations continue to work unchanged.

### Added

- **`sentience_governor.analyze` package.** New top-level subpackage
  for derived-metric analyzers.
- **`compute_undeclared_intent_spend(events)`.** Pure-function
  analyzer in `sentience_governor.analyze.undeclared_intent`. No I/O,
  no environment reads, no input mutation, byte-stable output.
  Single-pass O(n) algorithm with bounded memory. Verified safe for
  10k-event traces under 1s.
- **Turn-window bracketing model.** Surface-agnostic attribution
  algorithm that handles both the MCP wrapper's emit shape
  (`SCOPE_ASSERTED` → `CONTEXT_SNAPSHOT`) and the Claude Code hook's
  dual-snapshot shape (`CONTEXT_SNAPSHOT pre` → `SCOPE_ASSERTED` →
  `CONTEXT_SNAPSHOT post`) uniformly. Replaces the earlier
  pair-by-`tool_id` approach (the wrapper schema doesn't carry
  `tool_id` on `ContextSnapshotPayload`, making true pairing
  unimplementable).
- **`render_cli` and `render_markdown_report` renderers.**
  Pure-function renderers in `sentience_governor.analyze.renderers`.
  CLI output is one-screen-friendly, screenshot-worthy. Markdown
  report carries the canonical two-vector footer (direct reply path
  + launch-list link).
- **`sentience analyze undeclared-intent` CLI subcommand.** New
  subcommand group in `sentience` with positional target
  (session-id prefix OR file path OR omitted = `--latest`),
  `--latest`, `--json`, `--save`, `--no-prompt` flags.
- **Saved report flow.** Writes to
  `~/.sentience/reports/undeclared-intent-<sid-prefix>-<timestamp>.md`
  on operator confirmation or `--save`. P7-strict ordering: prompt
  fires only after the metric has rendered; suppressed for non-`ok`
  status; `--json` and `--no-prompt` suppress prompting
  unconditionally; saved path is echoed back to the operator.
- **Differentiated footer copy.** Both CLI and Markdown branch on
  `session_has_declared_intent`. The agent-bound copy is used when
  intent was declared; the surface-bound copy is used when no
  `INTENT_DECLARED` event fires anywhere in the session — framing
  the result as a surface-level limitation rather than agent drift.
- **Showcase examples.** Three pre-rendered scenarios under
  `examples/showcase/` (low / high / surface-bound). The fixtures
  are inlined in `examples/showcase/regenerate.py` so the script is
  the single source of truth and re-rendering is byte-stable
  (MD5-verified across consecutive runs).
- **Runnable demo.** `examples/v024_undeclared_intent_demo.py`
  builds a synthesized 4-turn session, runs the analyzer, and
  prints the CLI render, a Markdown excerpt, and the JSON output.
- **Userdocs §10 "Analyzers — derived metrics over captured
  traces".** Covers the CLI, the saved Markdown report, the JSON
  output schema, status branches, and the differentiated footer.

### Changed

- Userdocs sections 11-14 are renumbered (was 10-13) to make room
  for the new §10 "Analyzers". Section content unchanged.
- README "What's new" section refreshed for v0.2.4 with the v0.2.3
  block retained as historical context.
- `sentience` CLI help output now includes the `analyze` subcommand
  group.
- Test-suite count: 519 (was 488 in v0.2.3.post1).

### What this release is NOT

- **Not a schema bump.** No new event types, no new payload fields,
  no breaking changes.
- **Not enforcement.** v0.2.4 ships *visibility* — the metric
  exposes the gap. Intervention modes (review, constraint,
  confirmation, block) follow downstream.
- **Not a dashboard.** Single-session reports today. Consolidated
  views across runs are downstream.
- **Not a PDF report integration.** The PDF generator at
  `examples/sentience_business_report.py` (v0.2.3 cycle) is
  unchanged; the v0.2.4 metric is not surfaced through it.
  Consolidated visualization is a control-plane concern.

See `userdocs/sentience_governor.md` §10 for the full guide.

## [0.2.3.post1] — 2026-05-07

Metadata-only post-release. **No code changes** — same wheel
contents as 0.2.3. The PyPI landing page README is refreshed to
surface v0.2.3 features clearly:

- Opening paragraph now mentions execution-cost attribution as a
  first-class capability.
- New "What's new in 0.2.3" section near the top of the README
  with three short paragraphs covering execution-cost attribution,
  the first-run UX, and the LangChain + LangGraph additions.

Per PEP 440 conventions, post-releases are for metadata fixes
that don't justify a real version bump. `pip install
sentience-governor` and `pip install --upgrade sentience-governor`
both pick this up as the latest version.

## [0.2.3] — 2026-05-07

Set out to add token tracking. Ended up adding execution-cost
attribution: token spend attached directly to execution-boundary
traces, with per-turn identity so multi-tool-call attribution stays
mathematically correct. No breaking changes; existing v0.2.2 wrappers,
traces, and downstream tooling continue to work unchanged.

Unexpected token spend in agent systems is often an operational signal:
retries after failed tool calls, excessive reasoning before low-value
actions, undeclared execution, execution loops, or intent drifting from
action. v0.2.3 records the raw token facts needed to correlate those
behaviors with execution-boundary traces.

What we deliberately did NOT do:
- no schema bump
- no new event types
- no provider "normalization"
- no derived cost math
- no dashboards pretending Anthropic and OpenAI count tokens the same way

### Added

- **Launch-list email capture.** The `sentience` CLI prompts once on
  first invocation for an email so we can let you know when the hosted
  dashboard ships. Easy to skip; never re-asks. Also at
  `getsentience.ai/launch-list`. Architecturally separate from
  `sentience-sync` — the launch list never receives trace data,
  telemetry counts, or tool calls.

- **Optional LLM-token tracking on `CONTEXT_SNAPSHOT` events.** Eight
  new optional fields on `ClassificationHint` and
  `ContextSnapshotPayload`: `llm_prompt_tokens`, `llm_completion_tokens`,
  `llm_cached_read_tokens`, `llm_cached_write_tokens`,
  `llm_reasoning_tokens`, `model_identifier`, `provider`, `llm_turn_id`.
  All optional; non-adopters see zero schema change. Provider-accurate
  raw values pass through unchanged (Anthropic excludes cache from
  input; OpenAI includes; we don't reconcile).

- **Per-turn attribution identity (`llm_turn_id`).** When one LLM turn
  produces multiple tool calls, the same token usage attaches to every
  emitted event in that turn, all sharing one `llm_turn_id`. Without
  this identity, aggregation across multi-tool-call turns becomes
  mathematically wrong. Consumers MUST dedupe by
  `(session_id, llm_turn_id)` before summing canonical token fields.
  See `userdocs/sentience_governor.md` "Token tracking (optional)" →
  "Aggregation warning" for the full rule and per-provider cache-token
  semantics.

- **`SentienceCallbackHandler.on_llm_start` and `on_llm_end`.** New
  callback methods that capture per-turn token usage from LangChain
  responses and attach it (with the turn's `llm_turn_id`) to subsequent
  tool-call events. Trace immutability preserved: events emitted before
  `on_llm_end` arrives carry the turn id but no token fields, and are
  never mutated retroactively when usage data lands.

- **`SentienceMiddleware.awrap_step`.** New optional LangGraph hook that
  aggregates token usage across messages within a step. Existing
  `awrap_tool_call` is unchanged and remains backward-compatible for
  users who don't opt in.

- **Defensive token-extraction helper module**
  (`sentience_governor.wrapper.token_extraction`). Handles Anthropic
  native, OpenAI dict, LangChain `usage_metadata`, and legacy
  `llm_output["token_usage"]` shapes. Merges across shapes so
  Anthropic-via-LangChain cache fields are preserved (they would be
  silently dropped by an early-returning helper).

- Claude Code hook adapter probes the hook payload for `usage` /
  `token_usage` defensively. Anthropic does not currently expose this
  in the hook payload; fields stay `None` until they do, and no further
  changes are needed when they do.

### Changed

- `sentience status` reassurance line now reads *"Sentience is governing
  your Claude Code sessions locally."* (was "capturing your Claude Code
  sessions"). Better matches the product brand and makes the local-first
  privacy stance explicit.

### What this release is NOT

- Not a schema-bump release. Existing event types are unchanged; no
  new event types added.
- Not a cost-calculation feature. We record raw provider-reported
  tokens; cost math lives in dashboards / downstream tools.
- Not a token-budget enforcement feature. Governance based on token
  counts is separate work.
- Not a dashboard. The hosted dashboard and downstream analytics ship
  separately — see `getsentience.ai/launch-list` to be notified.

## [0.2.2] — 2026-04-28

Patch release. No API changes. No command changes.

### Fixed

- **Default network timeout for `sentience-sync` raised from 15 seconds
  to 30 seconds.** First-call latency on the cloud sync endpoint can
  exceed 15 seconds on slower client networks, causing
  `sentience-sync run` to fail with a network-error message even when
  the request would have eventually succeeded. The new default
  comfortably covers typical first-call latency while still failing
  fast on a genuinely unreachable network. Operators can override
  via the `SENTIENCE_SYNC_TIMEOUT_SECONDS` environment variable or
  the `timeout_seconds` config key.
- **Removed misleading `(placeholder)` suffix from `sentience-sync
  status` output.** The status command previously appended
  "(placeholder)" next to the production endpoint URL, suggesting
  configuration was incomplete when it wasn't. Output now shows the
  endpoint URL plainly.

### Changed

- **README and userdocs now recommend `pipx install sentience-governor`
  as the canonical install method.** Modern macOS and Linux Pythons
  enforce PEP 668 and refuse `pip install` outside a virtualenv;
  `pipx` is the standard fix. Library integration via
  `pip install sentience-governor` inside an active venv is still
  documented and supported. Updated install guide and troubleshooting
  cover both paths.

## [0.2.1] — 2026-04-26

Patch release. No API changes. No command changes.

### Fixed

- **Default network timeout for `sentience-sync` raised from 5 seconds
  to 15 seconds.** The previous default was too tight for first-call
  latency on the cloud sync endpoint and could cause `sentience-sync
  register` to fail with a network-error message on first use. The new
  default covers normal first-call latency comfortably while still
  failing fast on a genuinely unreachable network. Operators on slow
  links can raise the timeout further via the
  `SENTIENCE_SYNC_TIMEOUT_SECONDS` environment variable or the
  `timeout_seconds` config key.

### Notes

- macOS users running Python from python.org may see `SSL:
  CERTIFICATE_VERIFY_FAILED` on first network call. This is a known
  Python packaging quirk, not a Sentience issue. One-time fix:
  `/Applications/Python\ 3.13/Install\ Certificates.command` (adjust
  the version number to match your Python install). The troubleshooting
  guide covers this in detail.

## [0.2.0] — 2026-04-23

First release talking to a live production endpoint. The
governance runtime itself is unchanged from 0.1.9; the minor
bump is driven by a breaking change in the
`sentience-sync register` CLI surface.

### Breaking

- **`sentience-sync register` now requires `--email` and
  `--name`.** Scripts that invoke `register` without these
  flags will fail before any network call. This aligns the
  CLI with the server's contract — contact info is required
  because Sentience Sync is an opt-in communication channel.
- **The Sentience Sync local state file format is bumped from
  v1 to v2.** New fields: `installation_secret`,
  `contact_email`, `contact_name`. No manual migration needed
  — v1 state files are loaded cleanly, new fields default to
  empty, and the file is re-written as v2 on the next
  successful save. Upgrading from 0.1.9 is transparent.

### Added

- **Live production endpoint at `https://sync.getsentience.ai/v1`.**
  All three routes — `/register`, `/sync`, `/update-check` —
  are now served by a real backend. Previous versions shipped
  with a placeholder URL.
- **Bearer-token authentication on `/v1/sync`.** The
  `installation_secret` returned at register time is stored
  locally and presented on every subsequent sync call.
- **`user_agent` field in registration payloads.** Identifies
  CLI version + Python version + OS for server-side
  observability.
- **Duplicate-sync contract.** Re-running `sentience-sync run`
  for a window that was already uploaded is now a successful
  no-op. The server returns `duplicate=true`, the CLI prints
  `Already uploaded for this window (sync_run_id=<id>)`, and
  exits 0. Expected behaviour for cron retries.
- **Targeted error messages.** Every expected failure path
  surfaces a specific, actionable message:
  - "Not registered. Run `register` first." — for a clean
    first run
  - "Registration incomplete or outdated. Run `register` again."
    — for an upgrade-from-0.1.9 or corrupted state
  - "Authorization failed. Your installation_secret may be out
    of sync. Run `register` again (your installation ID will
    be preserved)." — for a 401 response
- **State-file permission hardening on POSIX.** The state file
  is written with owner-only permissions (`0600`). On Windows,
  best-effort equivalent with a warning if unavailable.
- **Optional `--organization` and `--role` flags on register.**
  For teams that want to tag their installation with more than
  just the operator name.

### Changed

- **Default endpoint is live, not a placeholder.** The
  "placeholder endpoint" caveat is gone from every surface —
  documentation, CLI help output, code comments.
- **`/v1/update-check` is now a GET with query string** (was a
  POST with a body). No auth required. Matches the server's
  public API.
- **Payload shapes match the server contract exactly.**
  Internal fields (`language_binding`, `deployment_label`)
  that were never adopted server-side are dropped from the
  wire format.

### Security

- **`installation_secret` stored in owner-readable JSON.** This
  is best-effort local hardening — meaningful protection
  against accidental exposure (other local users, backup
  tarballs) but not a defense against local root. A
  compromised secret can be rotated without creating a
  duplicate installation.
- **Bearer token never appears in the request body.** Only in
  the `Authorization` header.
- **Source IPs hashed server-side.** The raw source IP of a
  sync request is SHA-256 hashed with a server-side pepper on
  arrival and never stored.

### Correctness invariant

- **`register` never regenerates your `installation_id`** once
  one exists locally. `--force` refreshes the secret but
  preserves the ID. This keeps your history intact across
  re-registrations and makes the server's recovery flow work
  cleanly without creating duplicate rows.

### Upgrade notes from 0.1.9

1. `pip install --upgrade sentience-governor`
2. Run `sentience-sync register --email you@example.com --name "Your Name"`
3. Your existing `installation_id` is preserved; a fresh
   `installation_secret` is issued and stored locally
4. Any scripts that invoked `sentience-sync register` without
   flags will need to be updated — the flags are now required

Everything else continues to work. Per-session traces,
`sentience` / `sentience-cli`, the Claude Code hook, and all
library-integration paths are unchanged.

## [0.1.9] — 2026-04-17

Public release of the Sentience Governor open-tier runtime,
including execution-boundary instrumentation, policy evaluation, local
trace generation, and CLI tooling.

### Added
- Core governance runtime with 5 control points:
  AGENT_REGISTERED, INTENT_DECLARED, SCOPE_ASSERTED,
  CONTEXT_SNAPSHOT, MEMORY_WRITE_ATTEMPT
- GOVERNANCE_ERROR event for runtime faults (sink failure, schema
  violation, intercept failure, timeout)
- MCP wrapper path (`wrap_mcp_client`) with `SentienceMCPAdapter`
- LangChain adapter: `SentienceCallbackHandler` and
  `SentienceMiddleware`
- Honest intent classification: inferred inputs are marked
  `intent_source=inferred` with `intent_confidence=inferred_low`,
  never `explicit`
- EventBuilder with 5 default policy rules (POL-001 through
  POL-005) and 8 advisory flags
- Session manager with IDLE → ACTIVE → CLOSING → CLOSED lifecycle
- In-process per-session cache for intent baseline and sensitivity
  tier tracking
- Three sinks: StdoutSink, FileSink, HttpLocalSink with fail-open
  semantics
- CLI trace viewer (`sentience-cli`) supporting NDJSON and JSON
  array input formats
- Sentience Sync CLI (`sentience-sync`) for optional, explicit,
  opt-in telemetry
- Wrapper-based integration model requiring no modification to
  agent logic
- Open-tier design: observe-only, fail-open runtime with no
  network calls or execution blocking
- Claude Code hook adapter
  (`sentience_governor.wrapper.claude_code_hook`) with
  `sentience-claude-code-hook` CLI entry point. Zero-code
  integration via `.claude/settings.json` produces governance
  traces for every Claude Code tool invocation (`Bash`, `Edit`,
  `Write`, `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`,
  `Agent`, and every `mcp__<server>__<tool>`). Observe-only,
  fail-open, no credentials required.
- **Per-session trace files by default** for the Claude Code
  hook. Default sink is now a directory
  (`~/.sentience/traces/claude-code/`) with one `<session_id>.jsonl`
  file per Claude Code session, bounding growth naturally. Setting
  `SENTIENCE_CLAUDE_CODE_SINK_PATH` to a path ending in `.jsonl`
  keeps the legacy shared-file mode for operators who want every
  session in one interleaved trace.
- `sentience-cli` now accepts a directory argument: it globs
  `*.jsonl` inside, merges the sessions, and renders them in one
  pass. Single-file usage unchanged.
- Sink-backed session resumption primitive
  (`sentience_governor.session_manager.resumption`): atomic
  sidecar index, forward-validation, linear-scan fallback,
  file-locked read-plus-append critical section. Enables chain
  continuity across fresh Python processes (used today by the
  Claude Code hook; reusable by future adapters that need
  process-restart survival).
- `SessionManager.session_start` gains optional
  `initial_sequence` and `initial_last_event_id` parameters for
  pre-seeding chain state when resuming from disk. Defaults
  preserve the original fresh-session behaviour for every
  existing caller.
- **New `sentience` top-level CLI** (subcommands: `status`, `list`,
  `open`). Purpose-built for the agent-hook workflow where a single
  Claude Code session can produce hundreds of tool calls. Uses a
  baseline-noise classifier (pattern match + >80% frequency
  threshold) to separate real anomalies from expected noise, then
  renders a six-block output: Header, Summary (with `Status: ⚠` or
  `Status: ✓` anchor), Focus (plain-English "what to pay attention
  to"), Notes (baseline framing, shown only when relevant), Key
  Events (anomalies only, max 10 with plain-English glosses), Full
  Trace (one-liner per event), and Footer (tips + raw JSONL path).
  The existing `sentience-cli` stays unchanged for library/MCP/
  LangChain/golden-trace use — two-CLI architecture by design.
- `sentience open --latest --summary` — skips the Full Trace block
  so the rendered output fits on one terminal screen for busy
  sessions. Every other block prints as usual; the JSONL file on
  disk is unchanged.
- Locked gloss table and event formatting table implemented in
  `sentience_governor/cli/ux.py`. Unknown tools render with tool
  identity preserved (`<tool_name> → ???`), enabling forward-
  compatibility with future coding-agent adapters without code
  changes.
