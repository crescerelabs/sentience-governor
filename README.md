# Sentience Governor

**Local-first governance for AI agents at runtime.**

Sentience Governor captures agent actions at the execution boundary, evaluates
them against declared intent, scope, and policy, and writes a verifiable local
record of each captured tool call.

When an agent operates outside what it declared, Sentience Governor surfaces the
policy violation and attributes the associated token usage to the turn where it
happened.

[![PyPI](https://img.shields.io/pypi/v/sentience-governor.svg)](https://pypi.org/project/sentience-governor/)
[![Python](https://img.shields.io/pypi/pyversions/sentience-governor.svg)](https://pypi.org/project/sentience-governor/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/crescerelabs/sentience-governor/blob/main/LICENSE)
[![test](https://github.com/crescerelabs/sentience-governor/actions/workflows/test.yml/badge.svg)](https://github.com/crescerelabs/sentience-governor/actions/workflows/test.yml)

No account, no API key. By default, your traces stay on your machine.

| Property | Specification |
| :-- | :-- |
| Software entity | Sentience Governor (`sentience-governor`) |
| Category | AI agent runtime governance and execution recording |
| Execution architecture | Execution-boundary hook, plus an opt-in local MCP server (stdio) |
| Core artifact | Sentience Agent Execution Record (local event stream) |
| Integrations | Claude Code, MCP clients, LangChain, LangGraph |
| Default stance | Observe-only, fail-open, no telemetry, local-first |

![Sentience Governor: an agent's declared intent on the left, its runtime actions on the right, each marked within scope, outside scope, or a policy violation, with token spend attributed to each action](https://raw.githubusercontent.com/crescerelabs/sentience-governor/main/docs/assets/demo.gif)

*Illustrative. Sentience Governor records and evaluates a session, then reports
on it; the open-source release does not adjudicate or block actions as they
happen. For real output, see [See it work](#see-it-work) below.*

---

## What's new

**0.3.1.2** gives the review context when you ask for it inside Claude Code.
The summary now names every session it reviewed, not just the ones that stood
out, and says for each whether a finding was retained, was not, or cannot be
established because the display was truncated. Cross-project findings say how
many distinct files the writes touched, not just how many write operations
there were, which is the difference between many edits to one file and one edit
to many. Local and read-only, as before.

**0.3.1.1** gives the retrospective review an evidence path. The summary in
0.3.1 could tell you a session wrote into another project directory, and how
many times, and then stopped; `sentience scan --detail` now shows the paths
behind that number,
grouped by session, and the summary names that path itself so it is not
something you have to discover from `--help`. The same review and the same
evidence are available through the opt-in MCP server, so you can ask for them
inside a Claude Code session and get the evidence back in the conversation.

Same scan, same window, same findings: `--detail` is depth, never a broader
search. Local and read-only, as before.

**0.3.1** added `sentience scan` — a retrospective review of the Claude Code
history already on your machine, reporting which sessions recorded write
activity outside the project they were working in, before you have declared an
intent or instrumented anything. The same review is available inside Claude
Code as `/sentience-review`. Full detail in the
[changelog](https://github.com/crescerelabs/sentience-governor/blob/main/CHANGELOG.md).

---

## Quickstart

```bash
pipx install sentience-governor
sentience init claude-code
sentience pulse --latest
```

Install the package, wire the Claude Code hook, and read the governance report.
Requires Python 3.10 or newer.

Restart Claude Code once after the first install. You can then run
`/sentience-pulse` inside a session instead of using the terminal.

<details>
<summary>Other install situations</summary>

**No pipx?** `brew install pipx` on macOS, or `python3 -m pip install --user pipx`
on Linux and WSL, then `pipx ensurepath` and restart your shell.

**Using it as a library** (MCP wrapper, LangChain callback, custom runtime):
`pip install sentience-governor` inside your project's virtualenv. The pipx path
above is for CLI use.

**With the MCP server:** see [MCP server](#mcp-server). The extra has to be
present in the same environment as the CLI, so the command differs between a
pipx install and a virtualenv install.

**Upgrade / remove:** `pipx upgrade sentience-governor`,
`pipx uninstall sentience-governor`.

Installs four commands: `sentience`, `sentience-cli`,
`sentience-claude-code-hook`, and `sentience-mcp-server`.

</details>

---

## See it work

No setup, no API key, nothing to configure:

```bash
sentience demo undeclared-intent
```

```text
Undeclared-Intent Spend — session demo-v02...
─────────────────────────────────────────────
Total compute           4,840 tokens
Undeclared              1,000 tokens   (20.7%)
Declared                3,840 tokens   (79.3%)

Undeclared turns
  Turn turn-3    slack.write_message        1,000 tokens   INTENT_MISSING,POL-001
```

The session is synthesized and shipped with the package. The analyzer, the
policy evaluation, and the attribution are the same ones that run against your
own captured sessions.

---

## The problem it solves

An agent says it is fixing a test. Twelve tool calls later it has edited a
config file, written to a path nobody mentioned, and posted to Slack. The
actions may all succeed, while nothing in the agent harness identifies the
drift as a governance violation.

Sentience Governor compares captured agent actions with the intent and scope the
agent declared. Activity that violates the active policy is recorded on the turn
where it happened, with the associated token usage attributed to that turn.

### Where it sits

Three different layers get called "AI safety," and they answer different
questions. Sentience Governor works at the third.

| Layer | What it evaluates | What it cannot tell you |
| :-- | :-- | :-- |
| Model-layer safety | The content a model produces: bias, toxicity, hallucination | What the agent then did with it |
| Application logging | That a tool call happened, with its arguments | Whether the call matched what the agent declared |
| **Execution-boundary governance** | **The action against the declared intent, scope, and policy** | **Whether the declaration itself was truthful** |

### What the record is

Sentience Governor writes a Sentience Agent Execution Record. A generic
execution trace answers *what happened during this run*; the Record also
carries *what the agent said it was going to do*, which is what makes
divergence visible at all.

| | Generic execution trace | Sentience Agent Execution Record |
| :-- | :-- | :-- |
| Captures | Tool calls, arguments, results | Governed events at the execution boundary |
| Declared intent | Not represented | Recorded as a first-class event, and untrusted |
| Scope | Not represented | Asserted, then compared against each action |
| Policy | External, if any | Five default rules evaluated per event |
| Token usage | Per request, if recorded | Attributed to the turn where the activity happened |
| Answers | What happened during this run | Whether the action matched what was declared |

---

## What you get

- Local capture at the execution boundary
- Intent, scope, and policy evaluation
- Policy violations tied to the turn where they occurred
- Token attribution for drifted turns
- CLI reports and opt-in MCP access

## What it does not do

- Block or modify agent execution
- Send traces, prompts, or usage data anywhere by default
- Aggregate governance state across sessions, machines, or a hosted plane
- Require an account, an API key, or a network connection to work
- Classify data unless your integration provides the classification

### Local-first by design

**No telemetry.** No usage beacon, no license check, no crash reporter, no
machine identifiers. Traces are files on your disk, and governance runs fine
with the network off.

The package has two explicit network-capable paths, neither of which is on the
default governance path:

- An optional sink that posts events to an operator-configured URL
  ([`sink/writer.py`](https://github.com/crescerelabs/sentience-governor/blob/main/sentience_governor/sink/writer.py)).
  The default sink writes locally.
- A one-time launch-list prompt that sends an email address only when the
  operator enters one
  ([`cli/first_run.py`](https://github.com/crescerelabs/sentience-governor/blob/main/sentience_governor/cli/first_run.py)).
  Press Enter to skip it permanently. It never asks twice and never prompts
  in CI.

Neither path sends governance data anywhere by default. Both are
Apache-licensed source you can read.

---

## Supported integrations

| Integration | Capture path | Support |
| :-- | :-- | :-- |
| **Claude Code** | Execution hooks and slash commands | Best-supported |
| **MCP clients** | Client wrapper (`wrap_mcp_client`) | Supported |
| **LangChain** | Callback handler (`SentienceCallbackHandler`) | Supported |
| **LangGraph** | Middleware (`SentienceMiddleware`) | Experimental |

All integrations produce the same local governance trace and are read by the
same Sentience commands.

**Claude Code**

```bash
sentience init claude-code              # current directory
sentience init claude-code ~/some/proj  # a specific project
sentience init claude-code --mcp        # also register the MCP server
```

Writes the hook to the machine-local `<path>/.claude/settings.local.json`
(from 0.3.0.3; requires Claude Code v2.1.211+) and installs the Sentience
slash commands, including `/sentience-pulse`. Re-running — or running any
`sentience` command in the project — keeps the configuration current for your
install, so a reinstall or moved environment repairs itself on the next
command. `--project` installs the skills
into the project instead of your home directory, so they can be shared with a
team through git. `--no-skills` wires hooks only.

**MCP client wrapper.** This is the *client* side: you wrap your own MCP client
so its tool calls are captured. It is not the same thing as the
[MCP server](#mcp-server) below, which is how Claude queries Sentience.

```python
from sentience_governor.wrapper import wrap_mcp_client

governed = wrap_mcp_client(
    client,
    call_fn=lambda delegate, name, args: delegate.call_tool(name, args),
)
```

`call_fn` is the only SDK-aware part. Everything else is transport-agnostic.

**LangChain and LangGraph.** Attach `SentienceCallbackHandler` to your agent's
callback list, or `SentienceMiddleware` for `create_react_agent` shapes. Tool
calls are captured at the same boundary, into the same trace format, and read
by the same commands.

Runnable versions of all of these live in
[examples/](https://github.com/crescerelabs/sentience-governor/tree/main/examples/).

---

## How it works

1. A supported hook, wrapper, or callback captures an agent action at the
   execution boundary.
2. Sentience Governor writes the action as a structured event in a local trace.
3. The event is evaluated against declared intent, scope, and active policy.
4. CLI commands and the opt-in MCP server expose violations, attribution, and
   session status.

Six governed event types are recorded:

| Event type | Recorded when |
| :-- | :-- |
| `AGENT_REGISTERED` | An agent identifies itself to the session |
| `INTENT_DECLARED` | An objective is declared for subsequent activity |
| `SCOPE_ASSERTED` | The operation targets that declaration authorizes are stated |
| `CONTEXT_SNAPSHOT` | Data enters the agent's context |
| `MEMORY_WRITE_ATTEMPT` | A write to a persistence target is attempted |
| `GOVERNANCE_ERROR` | Capture itself degrades: a capture failure, an unavailable sink, a schema violation, or a timeout |

`GOVERNANCE_ERROR` is why the record can be read as evidence: when governance
degrades, the gap is recorded rather than silently absent.

---

## MCP server

Sentience Governor includes an opt-in MCP server that allows Claude to access
governance information from inside a session, and to review the Claude Code
history already on your machine.

Completed-session analysis reads the last completed session. The current
session exposes structural status only because token analysis is not final
until the session ends.

The server needs the `[mcp]` extra, in the same environment as the CLI:

```bash
pipx install "sentience-governor[mcp]"
sentience init claude-code --mcp
```

Already installed the base package with pipx? Reinstall with the extra:

```bash
pipx install --force "sentience-governor[mcp]"
```

Installing into a virtualenv rather than pipx: `pip install "sentience-governor[mcp]"`.

Off by default. The flag is the consent.

Seven governance tools read and write the Sentience session itself:

| Governance tool | What Claude gets |
| :-- | :-- |
| `sentience_explain` | How every number is counted, including the attribution boundary |
| `sentience_profile_view` | The active governance profile |
| `sentience_pulse` | Last completed session's pulse |
| `sentience_intent` | Last completed session's declared intent |
| `sentience_violations` | Last completed session's policy violations |
| `sentience_session_status` | Current session, structural counts only |
| `sentience_declare_intent` | The agent states its objective and scope |

The retrospective Reader is a separate capability, not an eighth governance
tool. The seven above govern a Sentience session. `sentience_scan` reviews
another system's records — the Claude Code history already on your machine —
and no governance session is involved:

| Reader capability | What Claude gets |
| :-- | :-- |
| `sentience_scan(detail, since)` | The retrospective review; `detail=True` returns the evidence behind it, grouped by session (new in 0.3.1.1) |

Three deliberate constraints worth knowing before you rely on it:

- **Every read identifies its source session.** A last-completed-session read
  can never be mistaken for the live session. The current session exposes
  structural counts only; token analysis stays unavailable until the session
  ends, because the numbers are not final until then.
- **`declare_intent` is the only write, and it is not retroactive.** Matching
  activity after the declaration stops firing POL-001; earlier events keep
  their violations. It is recorded as agent-declared and content-untrusted,
  never as an operator instruction. An agent cannot clear its own history.
- **The review is retrospective, not live governance.** `sentience_scan` reads
  Claude Code's own history and reports what it records. It cannot establish
  what you intended or authorized, or whether an action complied with policy,
  and it writes nothing in any mode.

---

## Default policy rules

| Rule | What it checks |
| :-- | :-- |
| POL-001 | Agent must declare intent before executing mutating operations |
| POL-002 | Agents must be registered before accessing tools |
| POL-003 | Data entering context must be classified |
| POL-004 | Memory writes must carry classification and retention policy |
| POL-005 | Sensitive data must not escalate in context without explicit authorization |

All five rules are evaluated against the governance events captured in each
supported session. Violations are reported, not enforced.

---

## Honest limits

Governance tooling that oversells itself is worse than none, so:

- **Declared intent is untrusted input.** Sentience can identify when captured
  actions diverge from what an agent declared. It cannot determine whether the
  declaration itself was truthful or complete, or infer the agent's underlying
  motives. Recording an unsafe action correctly does not make the action safe
  or the agent trustworthy.
- **Sentience governs actions, not model behavior.** It evaluates observable
  agent actions in business and operational workflows. It does not detect bias,
  toxicity, hallucinations, harmful content, or other model-output and
  content-safety issues.
- **Attribution stops at the turn.** The model meters tokens per turn, not per
  tool. Every number is "tokens on turns involving tool X," never per-tool
  guesswork. `sentience explain` spells out the counting rules.
- **The current open-source release does not block.** It records and reports
  violations but does not stop or modify agent actions.
- **Coverage differs by harness.** See the support column above.

---

## Commands and documentation

```bash
sentience status                  # is the hook capturing?
sentience list                    # captured sessions, newest first
sentience pulse --latest          # drift, violations, and token burn in one report
sentience open --latest --summary # event by event
sentience explain                 # how every number is counted
sentience profile view            # the active governance profile
sentience demo declare-intent     # the POL-001 before/after flip
```

| | |
| :-- | :-- |
| **Command reference** | [docs/commands.md](https://github.com/crescerelabs/sentience-governor/blob/main/docs/commands.md) |
| **Claude Code quickstart** | [docs/quickstart.md](https://github.com/crescerelabs/sentience-governor/blob/main/docs/quickstart.md) |
| **Architecture** | [docs/ARCHITECTURE.md](https://github.com/crescerelabs/sentience-governor/blob/main/docs/ARCHITECTURE.md) |
| **Full docs** | [docs/index.md](https://github.com/crescerelabs/sentience-governor/blob/main/docs/index.md) |
| **Changelog** | [CHANGELOG.md](https://github.com/crescerelabs/sentience-governor/blob/main/CHANGELOG.md) |
| **Examples** | [examples/](https://github.com/crescerelabs/sentience-governor/tree/main/examples/) |

---

## Questions and support

- **Usage questions and ideas:** [GitHub Discussions](https://github.com/crescerelabs/sentience-governor/discussions)
- **Reproducible bugs:** [GitHub Issues](https://github.com/crescerelabs/sentience-governor/issues)
- **Integration contributions:** read [CONTRIBUTING.md](https://github.com/crescerelabs/sentience-governor/blob/main/CONTRIBUTING.md)
- **Security vulnerabilities:** follow the private route in [SECURITY.md](https://github.com/crescerelabs/sentience-governor/blob/main/SECURITY.md)

## Contributing

Integrations, harness support, examples, reporting and developer-experience
work, docs, and reproducible bug fixes are genuinely wanted. Governance
semantics (what counts as a violation, what an event means) are
maintainer-governed and need an issue and design agreement before
implementation.

Read [CONTRIBUTING.md](https://github.com/crescerelabs/sentience-governor/blob/main/CONTRIBUTING.md)
first. It sets out the project roles and what needs agreement up front, so you
do not spend a weekend on something that was never going to land.

## Security

Do not open a public issue for a vulnerability. See
[SECURITY.md](https://github.com/crescerelabs/sentience-governor/blob/main/SECURITY.md)
for the private disclosure route and what is in scope.

Traces can contain prompts, tool arguments, and filesystem paths from real
work. Redact before sharing one in an issue.

## License

Apache License 2.0. See
[LICENSE](https://github.com/crescerelabs/sentience-governor/blob/main/LICENSE).

Local capture, policy evaluation, violation reporting, and token attribution
for one operator on one machine ship under Apache 2.0.
