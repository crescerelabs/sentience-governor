# Changelog

All notable changes to `pydantic-ai-governor` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this distribution adheres to [Semantic Versioning](https://semver.org/).

**This changelog covers `pydantic-ai-governor` only.** `sentience-governor`
versions and releases on its own series, keeps its own changelog, and its
releases do not appear here.

## [0.1.0] — unreleased

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
