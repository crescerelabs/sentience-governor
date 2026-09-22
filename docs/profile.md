# Governance Profiles

A governance profile is an operator-authored YAML file that encodes what
you expect of an agent on this machine. It's the machine-readable sibling
to the agent's `CLAUDE.md` recipe: same expectations, two surfaces.

The machine default lives at `~/.sentience/profile.yaml`. Since 0.3.2 you
can also bind different profiles to different agents through
`~/.sentience/resolution.yaml`, and every governed session records which
profile governed it.

## What a profile shapes

A profile controls four things, all of them observability signals, not
enforcement:

1. **When undeclared intent is surfaced**: `session_intent.demand_at`
   takes one of `session_start`, `first_write`, or `never`. The
   wrapper fires `POL-001` per your choice.
2. **When a task boundary has been crossed**:
   `task_boundary.signals` is a list of any of `dir_change`,
   `file_type_shift`, `time_gap`, `read_to_write_transition`. The
   wrapper attaches `TASK_BOUNDARY_CROSSED` to the next
   `SCOPE_ASSERTED` when a signal fires.
3. **Which tools should be treated as high-consequence**:
   `high_consequence.tools` is a list of regex patterns matched
   against `<tool_id>:<target_system>`. Matches attach
   `HIGH_CONSEQUENCE_DETECTED` to that event.
4. **Which shell operations should be treated as high-consequence**
   (0.3.2): `high_consequence.operations` is a list of rules over what a
   Bash command actually does (`domain`, `action`, `destructive`). A rule
   that one classified effect satisfies attaches the same
   `HIGH_CONSEQUENCE_DETECTED`. See [Operations rules](#operations-rules-032).

## Quickstart

```bash
sentience profile init       # create starter profile (inline-commented)
sentience profile view       # inspect it
sentience profile edit       # tune it ($VISUAL/$EDITOR, else nano/vim/vi, else macOS TextEdit)
sentience profile validate   # schema and runtime-field check (read-only)
```

Sessions started after the file exists pick it up automatically: no
environment variable, no flag. Every event in a governed session carries
`profile_fingerprint` at the envelope level so traces correlate back to the
profile that produced them.

## Which profile governs a session (0.3.2)

Resolution happens **once per session** and is keyed on the session's
`agent_id`. It is the same for every runtime surface:

1. **Binding.** If `~/.sentience/resolution.yaml` exists, its `bindings`
   are scanned in file order and the **first** pattern that matches the
   `agent_id` is authoritative. Patterns use shell-style globs
   (`fnmatch`), case-sensitively. Paths are relative to the resolution
   file's directory unless absolute; `~` expands.
2. **Default.** With no match, or no resolution file, the machine default
   `~/.sentience/profile.yaml` applies if it exists.
3. **None.** Otherwise the session runs without a profile, exactly as
   before 0.2.5.

```yaml
# ~/.sentience/resolution.yaml
schema_version: 1
bindings:
  - agent_id: "claude-code-*"
    profile: profiles/strict.yaml        # relative to this file
  - agent_id: "deploy-bot"
    profile: ~/.sentience/profiles/deploy.yaml
```

**Degraded, not fall-through.** If the first matching binding's profile
cannot be loaded (missing file, malformed YAML) or is not valid (see
below), the resolution is `degraded`: the session falls back to the machine default when one
exists, and to no profile otherwise. **A later binding is never
consulted.** The chosen binding is the operator's decision; a broken file
is surfaced as a warning and in the trace, not silently replaced by the
next rule.

A malformed resolution file is ignored with a warning (the default step
still applies); a malformed binding entry is skipped while the rest of the
file is used.

**Invalid profiles never reach a session (0.3.2.1).** Loading a profile
checks the fields the runtime consumes, not only the shape of the file:
`schema_version` must be an integer; `task_boundary.signals` and
`high_consequence.tools` must be lists whose entries are strings;
`task_boundary.time_gap_seconds` must be a finite number of at least 0 and
`task_boundary.dir_change_depth` an integer of at least 1; each section
must be a mapping; and every mapping key must be a string.
`sentience profile validate` reports each of these as an error (a `tools`
pattern that does not compile as a regular expression is a warning; the
runtime skips it). A profile with any such error is treated exactly like
one that could not be loaded:

- **Bound to it:** the resolution is `degraded`, the warning names the
  file and the first errors, no later binding is consulted, and the
  session runs on the machine default when one exists and on no profile
  otherwise.
- **As the machine default:** the session runs without a profile. The
  resolution is `none` (or `degraded` when a binding had matched), the
  warning names the file, and `AGENT_REGISTERED` records no
  `profile_loaded`. Before 0.3.2.1 an unparseable default raised into the
  MCP wrapper's `async with` and left a LangChain run ungoverned with no
  warning; both now continue with the warning and the record.
- **Passed directly to `session_start`** (an integration that constructs
  its own `GovernanceProfile`): nothing is activated, one warning names the
  session and the errors, and the call returns normally.
- **A Claude Code snapshot from an earlier release** that rebuilds into an
  invalid profile is not used; the hook resolves again as a new session
  would and leaves the snapshot file in place.

Nothing raises into the governed application and nothing is blocked: the
run proceeds on the fallback, the warning says why, and the registration
records what was actually activated. Should a malformed value still reach
the runtime by some other path, each consumer substitutes the default the
validator requires rather than deriving policy from the malformed value,
and logs that field once per session. Valid parts of the same profile keep
applying: a `dir_change` signal still detects boundaries when
`dir_change_depth` is invalid (at the default depth), while an invalid
`time_gap_seconds` detects nothing rather than firing on every event.

**Provenance in the trace.** When a session resolved through a binding,
its `AGENT_REGISTERED` payload carries `profile_resolution` (`bound` or
`degraded`) and `profile_binding` (the matched pattern). Sessions on the
machine default or on no profile omit both fields, so their registrations
are byte-identical to earlier releases.

**Sticky by construction.** Resolution is a pure function of the
configuration at the moment the session opens. Editing
`resolution.yaml`, the bound file or the default mid-session changes
nothing for a session that is already open; the next session picks the
change up.

### Sticky sessions across processes (Claude Code)

The Claude Code hook runs a **fresh process for every tool call**, so
stickiness cannot live in memory. The first process for a session
resolves the profile, writes its canonical bytes to a **content-addressed
snapshot** beside the trace (`<trace dir>/profiles/<sha256>.json`, named by
the full SHA-256 of its bytes, verified before it is used, never rewritten
once valid), and records a small binding for the session in the trace's
sidecar. Every later process for that session rebuilds the identical
profile from the snapshot instead of re-reading the configuration, checks
the full hash first and the fingerprint second, and confirms the rebuilt
profile agrees with the session's own registration.

If the sidecar or snapshot is lost, the hook recovers **fail-open**: it
resolves again and compares with the registration. When they agree the
binding is silently re-materialized. When they disagree (the configuration
changed while the state was lost) the session continues on the fresh
profile, one warning is logged, and the binding is marked `reresolve` so
the switch is visible; later processes honour that binding as sticky
rather than warning again. The only case in which a session's later
events can carry a different fingerprint than its registration is that
one, and it is marked.

The MCP wrapper and the LangChain handler run one process per session, so
they resolve once at session start and are sticky by construction; they
write no sidecar and no snapshot.

**`pydantic-ai-governor`** 0.1.0 pins core to versions below 0.3.2 and
binds no resolved profile. Per-agent resolution for Pydantic AI arrives in
a separate companion release, 0.1.1, which follows core 0.3.2.1; it is not
part of this release.

### Diagnostics

Two read-only commands report what the runtime did without writing
anything (no snapshot, no binding, no trace):

```bash
sentience profile resolve --agent-id deploy-bot     # what a NEW session for this agent would get
sentience profile resolve --session-id <session>    # what an existing Claude Code session is bound to
sentience profile snapshots                          # the snapshot files beside the traces
```

`resolve --agent-id` prints the resolution (`bound`, `degraded`,
`default`, `none`), the matched binding, the source path, the 12-character
fingerprint, the full content hash and any warnings; it always exits 0.
`resolve --session-id` verifies the session's binding and snapshot and
reports one status: `OK`, `NO_BINDING` (a session from before 0.3.2, or
never established), `SNAPSHOT_MISSING`, `SNAPSHOT_CORRUPTED`,
`BINDING_INVALID`, `DISAGREES_WITH_REGISTRATION` (the fingerprint, or a
recorded resolution or binding, differs from the session's registration)
or `NO_TRACE`. It exits 0 for `OK` and `NO_BINDING` and 1 for any other
status, the same convention as `profile validate`. `snapshots` lists every
snapshot with its full content hash, fingerprint, size, whether the bytes
still hash to the name, and how many session bindings reference it; it
exits 0 and repairs nothing. `--json` is available on all three.

## Fingerprint contract (0.3.2)

`profile_fingerprint` is the first 12 hexadecimal characters of the
profile's canonical content hash. Since 0.3.2 the contract is explicit:

- **Existing profiles keep their fingerprint.** A profile written before
  `high_consequence.operations` existed has the same fingerprint under
  0.3.2 as before. The runtime's own defaults gained the new field, but
  optional additive fields at their absent-equivalent value are omitted
  from the canonical form, so nothing you did not write changes your
  fingerprint.
- **Absent and explicit empty are the same.** A profile with
  `operations: []` and one without the key have the same fingerprint.
- **Operation rules are an unordered set.** Reordering rules, or
  writing the same rule twice, does not change the fingerprint; adding,
  removing or changing a rule does.
- **List-valued predicates are sets.** In a rule, `domain: [a, b]` and
  `domain: [b, a]` are the same rule; duplicated members are the same
  rule.
- **Existing ordered fields keep their historical semantics.**
  `task_boundary.signals` and `high_consequence.tools` are ordered lists,
  as they always were; reordering them still changes the fingerprint.
- **Public evidence carries the 12-character fingerprint only.** The full
  64-character SHA-256 is the runtime's storage and integrity identity
  (it names snapshot files and appears in the sidecar and in the
  diagnostic commands); it never appears in a trace event.

## Operations rules (0.3.2)

Claude Code `Bash` calls are classified semantically before they are
evaluated. The classifier is a bounded, deterministic set of rules over
the command text: it never runs anything, never reads files or the
network, never asks a model, and never guesses. What it can recognise it
records; what it cannot, it marks `unknown`.

### What a classification says

Every Bash `SCOPE_ASSERTED` event carries `operation_classification`:

```json
"operation_classification": {
  "classifier": "shell_rules",
  "classifier_version": 1,
  "complete": true,
  "destructive": null,
  "segments": [
    {"executable": "curl", "effects": [
        {"domain": "network",    "action": "read",   "destructive": false},
        {"domain": "filesystem", "action": "modify", "destructive": null}]}
  ]
}
```

- **Segments** are the command's parts in order (`a && b`, `a | b`,
  `a; b`, one line each). Neutral commands (`cd`, `echo`, `export`, ...)
  produce no segment unless they carry a file redirection.
- **Effects** are the things one segment does. A segment can have zero,
  one or many. `curl -o x URL` acquires content *and* writes a local
  file, so it has two effects. Every effect is asserted from the syntax
  of that one segment; nothing is aggregated across effects or segments,
  and there is no top-level domain or action.
- **`domain`** is one of `filesystem`, `version_control`, `packages`,
  `network`, `cloud_infrastructure`, `process`, `unknown`. **Domains
  describe material governance effects, not implementation transport.**
  `aws ec2 terminate-instances` is `cloud_infrastructure/delete` and
  nothing else: the HTTPS call to the provider's API is how it works, not
  what it does. `network` appears when network interaction *is* the
  command's purpose: `curl`, `wget`, `ssh`, `scp`, `git push`, `pip
  install` from an index, a provider or chart acquisition such as
  `terraform init` or `helm repo update`.
- **`action`** is one of `read`, `create`, `modify`, `delete`,
  `execute`, `unknown`. Actions are conservative about what the syntax
  makes possible: `cp a b` is `modify` because the destination may already
  exist; `touch` is `modify` because it creates or updates.
- **`destructive`** is three-state:
  - `true` requires strong deterministic syntax evidence of discarding or
    state-replacing behaviour (`rm`, `mv`, `sed -i`, `git reset --hard`,
    `git push --force`, `terraform destroy`, `kubectl delete`, `npm ci`);
  - `false` means the classified effect is deterministically
    non-destructive (`git status`, `git commit`, `mkdir`, `touch`,
    `pip install`, `aws … describe-*`);
  - `null` means syntax alone cannot establish it (`cp a b`, `curl -o`,
    `tar -x`, `terraform apply`, `kubectl apply`, `helm upgrade`, any
    `execute` or `unknown` effect).
- **`complete`** is `true` only when every part of the command was
  recognised and no unsupported construct was present. Command
  substitution (`$(…)`, backticks), subshells, brace groups and process
  substitution are detected but **never interpreted**: the outer effects
  are kept, one explicit `unknown/unknown/null` effect is added, and
  `complete` becomes `false`. `echo $(aws ec2 terminate-instances …)` is
  an unknown, never neutral and never a cloud effect. Unknown executables
  (`./deploy.sh`), opaque forms (`bash -c`, `python -c`, `eval`, `xargs`)
  and untabled subcommands are likewise explicit unknowns.
- **Redirections** are effects on the segment they attach to: `> f` and
  `2> f` are `filesystem/modify/null` (create or truncate), `>> f` is
  `filesystem/modify/false`, `< f` is `filesystem/read/false`; `2>&1` and
  other descriptor duplications, heredocs and herestrings are not effects.
  **The literal output target `/dev/null` is a sink and emits no
  filesystem effect** (`ls x 2>/dev/null` is a pure read; `curl -o
  /dev/null` is a pure network read). Only that literal: `/dev/stdout`,
  `/dev/tty` and other device paths keep the ordinary rule.
- The top-level `destructive` is `true` if any effect is `true`, `false`
  only when the classification is complete and every effect is `false`,
  and `null` otherwise.

### `target_system` for Bash

The legacy `target_system` on a Bash event is derived from the
classification:

```
shell/<domain>   iff the classification is complete AND exactly one
                 distinct domain appears across every effect of every segment
shell            otherwise
```

| Command | `target_system` |
| :-- | :-- |
| `git status` | `shell/version_control` |
| `cat a > b` | `shell/filesystem` (two effects, one domain) |
| `git status && git log` | `shell/version_control` |
| `ls x 2>/dev/null` | `shell/filesystem` |
| `curl -o x URL` | `shell` (network and filesystem) |
| `git status > f` | `shell` |
| `rm -rf /tmp && aws ec2 describe-instances` | `shell` |
| `./deploy.sh`, `echo $(x)` | `shell` (unknown) |

The classification object is authoritative; `target_system` remains the
coarse compatibility surface. For `high_consequence.tools` this means
`Bash:shell` continues to match every Bash call (the regex is unanchored
and `shell/version_control` starts with `shell`), while an end-anchored
`Bash:shell$` matches only plain `shell`. The composite stays
`<tool_id>:<target_system>`, two parts; there is no third segment.

### Writing operations rules

```yaml
high_consequence:
  operations:
    - domain: cloud_infrastructure
      destructive: true
    - domain: [filesystem, version_control]
      action: delete
    - domain: network
      action: modify
    - domain: unknown          # anything the classifier could not read
  on_match: flag
```

**A rule matches only when one individual effect satisfies every
predicate in that rule.** `domain` and `action` take a value or a list
(a set); `destructive: true` is satisfied only by `true` and `false` only
by `false`; a `null` effect satisfies neither. Predicates are never
combined across effects:

- `curl -o x URL` has `network/read/false` and `filesystem/modify/null`;
  `{domain: network, action: modify}` does **not** match it.
- `rm -rf /tmp && aws ec2 describe-instances` has `filesystem/delete/true`
  and `cloud_infrastructure/read/false`; `{domain: cloud_infrastructure,
  destructive: true}` does **not** match it.
- `git push --force` has `network/modify/false` and
  `version_control/modify/true`; `{domain: network, destructive: true}`
  does **not** match it.

A match attaches `HIGH_CONSEQUENCE_DETECTED`, the same flag the `tools`
patterns attach; both surfaces can fire on one event and produce the
flag once. A malformed rule is reported by `sentience profile validate`
and skipped at runtime. `on_match` is `flag`; nothing is blocked.

Operations rules apply to Claude Code Bash calls. MCP-wrapped tools,
LangChain tools and Pydantic AI tools are not classified this way in
0.3.2, so operations rules never fire for them; `tools` patterns do.

## Example

A complete runnable walkthrough (profile + agent recipe + generated
trace + generated analyzer report) lives in the source tree at
`examples/showcase/v025-closed-loop/`.

The analyzer's Markdown report gains three optional sections when a
profile is active:

- `## Profile`: fingerprint + schema version
- `## High-consequence operations`: table of matched tools per turn
- `## Task boundaries crossed`: table of boundary-crossing events

These sections are omitted when no profile metadata is present, so
earlier-shaped traces produce byte-identical reports.

## Integration

Profiles are transparent to every wrapper surface:

- **Claude Code hook**: resolves the profile once per session and keeps it
  sticky across the hook's processes (above).
- **MCP wrapper**: see [MCP integration](./integrations/mcp.md).
- **LangChain handler / middleware**: see
  [LangChain integration](./integrations/langchain.md).

## What this is not

Profiles in the open tier are observability, not enforcement. The
schema reserves vocabulary (`on_match: prompt | block | deny`) for
future paid-tier behavior, but in this release every `on_match` value
warns and falls back to `flag`. Classification records what a command
would do; it does not stop it.
