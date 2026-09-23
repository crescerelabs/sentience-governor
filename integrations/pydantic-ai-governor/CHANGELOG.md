# Changelog

All notable changes to `pydantic-ai-governor` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this distribution adheres to [Semantic Versioning](https://semver.org/).

**This changelog covers `pydantic-ai-governor` only.** `sentience-governor`
versions and releases on its own series, keeps its own changelog, and its
releases do not appear here.

## [0.1.1] — unreleased

**Which profile governs this run.** Until now every run under this
capability used the one machine default profile, if any. Each run now
resolves the governance profile the operator intended for its agent, through
the core package's own per-agent resolution, and records which one applied.
Requires `sentience-governor` 0.3.2.1 or later.

### Added
- **Per-agent profile resolution, once per run.** With
  `~/.sentience/resolution.yaml` present, the first `agent_id` pattern that
  matches the capability's `agent_id` is authoritative; with no match or no
  file, the machine default `~/.sentience/profile.yaml` applies; with
  neither, the run proceeds with no profile, exactly as 0.1.0 did. There is
  no `profile=` argument: which profile applies is the operator's decision.
  Resolution happens when the session opens and is fixed for the life of the
  run; editing the configuration mid-run changes nothing until the next run.
- **Provenance in the evidence.** The registration records
  `profile_resolution` (`bound` or `degraded`) and `profile_binding` (the
  matched pattern); runs on the default or on no profile record neither, so
  their registrations are identical to 0.1.0's. Every event of a run under an
  operator-authored profile carries that profile's 12-character
  `profile_fingerprint`. No content hash, snapshot file or sidecar is
  written: one run is one process, so the resolved profile is sticky by
  construction.
- **Profile-driven evaluation of this integration's events.** Once a profile
  is bound, the core package's existing profile logic applies exactly as it
  does for the core MCP wrapper: `session_intent.demand_at` gates the
  undeclared-scope violation (`POL-001`), task-boundary signals are evaluated
  over the declared `target_system` values of successive tool calls, and
  `high_consequence.tools` patterns are matched against
  `<tool name>:<target_system>` and attach the advisory
  `HIGH_CONSEQUENCE_DETECTED` flag.

### Changed
- **Core requirement is now `sentience-governor>=0.3.2.1,<0.3.3`** (was
  `>=0.3.1.2,<0.3.2`). 0.3.2.1 is the first core release whose profile
  validation covers every field the runtime reads and whose resolver never
  raises into a run or hands back a profile its validator rejects. The
  `pydantic-ai-slim` bound is unchanged at `>=2.37.0,<2.38`.

### Notes
- **Fail-open, once, visibly.** A matched binding whose file is missing,
  unparseable or not valid resolves `degraded` to the machine default and
  never to a later binding; an unparseable or invalid default leaves the run
  on no profile; a malformed resolution file is ignored and the default
  applies; a malformed binding entry is skipped. Each case is one
  `UserWarning` and one `GOVERNANCE_ERROR` record with `agent_continued: true`
  at session open, and nothing more during the run. Nothing raises into the
  Pydantic AI run. As a second check, a profile the resolver hands back is
  used only if it exposes its sections and fingerprint and passes the core
  validator; otherwise the run continues with no profile and the registration
  says so. The core validator is the only judge of validity; this package
  encodes no schema rules.
- **Operations rules do not apply to Pydantic AI tool calls.**
  `high_consequence.operations` rules match the semantic classification the
  core package derives for Claude Code `Bash` commands
  (`operation_classification`). This integration records developer-declared
  execution evidence and emits no such classification, so those rules have
  nothing to match and never fire here; a profile carrying only operations
  rules still binds and is fingerprinted. `high_consequence.tools` patterns
  do apply. Nothing about tool classification changed in this release: the
  explicit-first metadata contract and the `UNKNOWN → READ` compatibility
  fallback on the legacy operation field are as in 0.1.0.
- **Observation only, as before.** A profile shapes what is evaluated and
  recorded. Nothing is halted, refused, delayed or altered. Across the tested
  execution paths, attaching the capability still does not change agent
  output, message count, token usage, exception propagation, retries,
  deferral, streaming or control flow.
- **Package metadata.** `Homepage` now points to `getsentience.ai`; `Source`
  and `Documentation` are unchanged.

## [0.1.0] — 2026-09-08

First release of the distribution.

### Added
- **`SentienceGovernor`, a Pydantic AI capability.** Attach it to an agent
  and each run opens its own Sentience Governor session, recording what the
  agent dispatched at runtime against the declaration state recorded before
  the run. The run's own `run_id` is the session id, so the two systems agree
  on identity without a mapping table.
- **Run declaration.** An `objective` and `scope` from the constructor as
  defaults, overridable per run through a `sentience_governor` metadata block.
  A malformed block is rejected atomically and visibly: the developer gets a
  warning naming the field and the contract it broke, valid constructor
  defaults remain in force, and only a run with no valid defaults is recorded
  as undeclared.
- **Explicit-first tool classification.** `operation`, `target_system` and
  `classification` on a tool's metadata. Nothing is inferred from a tool's
  name. An unclassified call is recorded as unclassified, with `target_system`
  falling back to the tool's own name.
- **The execution boundary.** A scope assertion after validation and
  immediately before dispatch, and a context snapshot after a normal return,
  joined by `tool_use_id`.
- **Token and model evidence.** One snapshot per model turn carrying measured
  usage, model and provider identity, and the tool call ids that turn issued,
  read from `ModelResponse.usage` rather than from a running total.
- **Concurrency and isolation.** Concurrent runs on one agent get separate
  sessions and separate traces; parallel tool calls in one model response
  carry distinct identities.

### Notes
- **Observation only.** Across the tested execution paths for this release,
  attaching the capability did not change agent output, message count, token
  usage, exception propagation, retries, deferral, streaming or control flow.
- **Compatibility bounds are deliberate**: `sentience-governor>=0.3.1.2,<0.3.2`
  and `pydantic-ai-slim>=2.37.0,<2.38`. They are widened only after measured
  verification, and only in a new release of this distribution.
