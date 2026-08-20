# Sentience Governor — User Guide

> **Audience:** operators and integrators who want to wrap an AI agent and produce governance traces.

## Contents

1. [What the Governor is](#1-what-the-governor-is)
2. [What problem it solves](#2-what-problem-it-solves)
3. [Five-minute quick start](#3-five-minute-quick-start)
4. [Core concepts](#4-core-concepts)
5. [Integration: wrapping an MCP-style client](#5-integration-wrapping-an-mcp-style-client)
6. [Integration: LangChain agents](#6-integration-langchain-agents)
7. [Integration: Claude Code sessions](#7-integration-claude-code-sessions)
8. [Injecting classification metadata via the hook](#8-injecting-classification-metadata-via-the-hook)
9. [The `sentience-cli` trace viewer](#9-the-sentience-cli-trace-viewer)
10. [Analyzers — derived metrics over captured traces](#10-analyzers--derived-metrics-over-captured-traces)
11. [Governance Profiles](#11-governance-profiles)
12. [Sentience Pulse](#12-sentience-pulse)
13. [Sinks: where governance events go](#13-sinks-where-governance-events-go)
14. [What the Governor does NOT do](#14-what-the-governor-does-not-do)
15. [Status, stability, and versioning](#15-status-stability-and-versioning)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. What the Governor is

Sentience Governor is a Python library that wraps an agent's execution boundary and produces a structured trace of what the agent did. It's the open-tier (free, source-installed, no account, local-first by default) implementation of the broader Sentience governance model.

The package ships:

- A **wrapper** (`sentience_governor.wrapper.mcp`) that intercepts agent tool calls and emits governance events
- A **LangChain adapter** (`sentience_governor.wrapper.langchain_adapter`) for LangChain-based agents
- An **EventBuilder** that runs the governance evaluation logic against each emitted event
- Three **sinks** for persisting events: stdout, file, and local HTTP
- A **CLI viewer** (`sentience-cli`) for reading the traces back

Everything lives in process. Governance runs with the network off; the two network-capable paths (an operator-configured sink, and the one-time launch-list prompt) are opt-in and neither is on the governance path.

`sentience-sync` was the experimental cloud-telemetry CLI. It was **sunset in v0.2.8.3** and the command was **removed in v0.3.0.1**.

### Mental model

- The Governor sits at the execution boundary.
- It captures supported agent actions at the execution boundary.
- It produces a structured trace — it does not enforce.

## 2. What problem it solves

Modern AI agents make tool calls that touch real systems — CRMs, databases, APIs. The questions enterprise IT teams ask about these agents are:

- *Who is acting?* (agent identity, version, ownership)
- *What did they intend?* (the agent's stated objective)
- *What tools did they call, in what order, with what permissions?*
- *What data ended up in the agent's context?* (and how sensitive was it?)
- *What did they try to persist?* (memory writes, retention, classification)

Most agent frameworks give you log lines. Sentience Governor gives you a structured, queryable trace with a defined schema, classification metadata, and policy evaluation. You can ship the trace as-is, pipe it into an audit pipeline, or visualise it with `sentience-cli`.

The Governor does **not** block agent execution. It observes. The story for "now actually prevent the bad thing" is the paid Sentience control-plane, which is out of scope for this package.

## 3. Five-minute quick start

### Install

For CLI usage (the three commands `sentience`, `sentience-cli`, `sentience-claude-code-hook` available globally on your `$PATH`):

```bash
pipx install sentience-governor
```

For **library integration** (you import `sentience_governor` as a Python module inside your own project — MCP wrapper, LangChain callback, custom agent runtime), install into your project's virtualenv:

```bash
pip install sentience-governor
```

The `pip install` path is for venv-scoped library use only. For CLI usage, use pipx.

See the [website install guide](https://getsentience.ai/docs/install/) for the canonical instructions.

### Wrap a trivial fake tool

Save this as `quickstart.py`:

```python
import asyncio
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import FileSink, SinkWriter
from sentience_governor.wrapper.mcp import (
    MCPClientLike,
    SentienceMCPAdapter,
    wrap_mcp_client,
)


# --- A fake tool: pretend it's your real CRM client ---
class FakeCRM:
    def call(self, name, args):
        if name == "crm.fetch_usage":
            return {"customer_id": args["id"], "calls": 12450}
        raise ValueError(f"unknown tool: {name}")


# --- Sentience collaborators ---
session_manager = SessionManager()
cache = InProcessCache()
sink = SinkWriter(FileSink("/tmp/quickstart.jsonl"))

# --- Wire your tool client into the Sentience adapter ---
adapted = SentienceMCPAdapter(
    delegate=FakeCRM(),
    call_fn=lambda client, name, args: client.call(name, args),
)

# --- Wrap it for governance ---
wrapped = wrap_mcp_client(
    target=adapted,
    session_manager=session_manager,
    cache=cache,
    sink_writer=sink,
    agent_id="quickstart-agent-v1",
    stated_objective="Fetch usage for one customer",
    declared_capabilities=["crm.read"],
)


async def main():
    async with wrapped:
        result = wrapped.send_tool_call("crm.fetch_usage", {"id": "cust-001"})
        print("tool returned:", result)


asyncio.run(main())
```

### Run it and view the trace

```bash
python quickstart.py
sentience-cli /tmp/quickstart.jsonl
```

You'll see a four-event trace: `AGENT_REGISTERED → INTENT_DECLARED → SCOPE_ASSERTED → CONTEXT_SNAPSHOT`, with a session summary at the bottom. The trace will note that the context snapshot has no classification metadata — that's expected without a hook (see §8 for how to fix that).

That's the whole loop. Everything below is going deeper.

### 3.1 Failure-first walkthrough: reading a bad trace

The quickstart above produces a relatively clean trace because it
supplies `agent_version`, `vendor_id`, `owner_claim`,
`declared_capabilities`, and `stated_objective`. It still shows
unclassified context unless you add a `classification_hook`. Most
first-time integrations start without all of those. Here's what that looks
like — and how to fix it.

**Minimal integration (no optional parameters):**

```python
wrapped = wrap_mcp_client(
    target=adapted,
    session_manager=session_manager,
    cache=cache,
    sink_writer=sink,
    agent_id="my-agent",
)
```

**The trace you'll see:**

```
[1] REGISTRATION          ⚠  Agent my-agent
    Advisory: AGENT_UNREGISTERED
    Policy violation: POL-002
    Consequence: This session would have been blocked at the central server.
    → Fix: Supply agent_version, vendor_id, owner_claim, or declared_capabilities

[2] INTENT                ⚠  No objective declared
    Advisory: INTENT_MISSING

[3] SCOPE                 ⚠  READ crm.get_customer → crm
    Advisory: SCOPE_INTENT_MISMATCH, SCOPE_OPERATION_UNEXPECTED
    Policy violation: POL-001
    Consequence: This WRITE operation would have been blocked.
    → Fix: Declare stated_objective and declared_capabilities

[4] CONTEXT               ⚠  classifications=[]  source=unclassified
    Advisory: CONTEXT_UNCLASSIFIED
    Policy violation: POL-003
    Consequence: Downstream tool calls requiring classified context would have been restricted.
    → Fix: Supply a classification_hook
```

Every event has a `⚠`. Four policy violations across four events
in this example. The Governor is telling you what's missing.

**Reading each flag:**

**`AGENT_UNREGISTERED` + `POL-002`** — the Governor has no metadata
about this agent. It doesn't know the version, the vendor, or who
owns this session. Fix: add `agent_version`, `vendor_id`,
`owner_claim`, and `declared_capabilities` to `wrap_mcp_client()`.

**`INTENT_MISSING`** — no session objective was declared. The
Governor can't compare what the agent does against what it's supposed
to do. Fix: add `stated_objective="..."` to `wrap_mcp_client()`.

**`SCOPE_INTENT_MISMATCH` + `POL-001`** — the tool call targets a
system (`crm`) that the agent never declared in its capabilities. The
Governor can't verify the agent is supposed to be touching this
system. Fix: add `declared_capabilities=["crm.read"]` so the scope
hint matches the tool call's target system.

**`CONTEXT_UNCLASSIFIED` + `POL-003`** — the tool returned data, but
no classification metadata was provided. The Governor doesn't know
if this data is public, internal, confidential, or restricted. Fix:
supply a `classification_hook` (see §8).

**After all four fixes:**

```python
wrapped = wrap_mcp_client(
    target=adapted,
    session_manager=session_manager,
    cache=cache,
    sink_writer=sink,
    agent_id="my-agent",
    agent_version="1.0.0",
    vendor_id="my-company",
    declared_capabilities=["crm.read"],
    owner_claim="user_123",
    stated_objective="Fetch customer data for Q1 report",
    classification_hook=my_classification_hook,
)
```

**The clean trace:**

```
[1] REGISTRATION          ✓  Agent my-agent (1.0.0)
[2] INTENT                ✓  Objective: 'Fetch customer data for Q1 report'  source=explicit
[3] SCOPE                 ✓  READ crm.get_customer → crm
[4] CONTEXT               ✓  classifications=[internal]  source=vendor  tokens=850
```

Zero violations. Same agent, same tool calls. The difference is
what you declare at integration time. A complete, explicit
integration produces a clean trace.

## 4. Core concepts

### 4.1 Five control points

The Governor emits an event at each of these moments in an agent session:

| Event type | When it fires | What it carries |
| :-- | :-- | :-- |
| `AGENT_REGISTERED` | At session start | `agent_id`, `agent_version`, `vendor_id`, `owner_claim`, `declared_capabilities` |
| `INTENT_DECLARED` | At session start, or first tool call if extracted from invocation context | `stated_objective` (the declared text), `intent_source` (`explicit` if integrator-supplied, `inferred` if extracted from inputs, `none` if absent), `intent_confidence` |
| `SCOPE_ASSERTED` | Before each tool call | `tool_id`, `target_system`, `operation_type` (READ/WRITE/DELETE/EXECUTE), `asserted_permissions` |
| `CONTEXT_SNAPSHOT` | After each tool call | `data_classifications`, `classification_source`, `provenance`, `retention_flags`, `context_size_tokens` |
| `MEMORY_WRITE_ATTEMPT` | After each tool call IF the call is to a persistence target | `target_store`, `write_type`, `write_classification`, `write_size_tokens`, `retention_requested` |

A sixth event type, `GOVERNANCE_ERROR`, fires when the runtime itself encounters a fault (sink unreachable, schema violation, intercept failure). It always goes to stdout regardless of the configured sink, and never interrupts the agent.

> **`context_size_tokens` is a per-snapshot context/payload size, not model token usage.** It records the size of the context captured in a single `CONTEXT_SNAPSHOT` event. It is *not* the model's prompt/completion token spend for the turn — per-turn model token usage (prompt, completion, cached read, cached write) is captured separately in the Claude Code token-burn fields and surfaced by `sentience pulse`.

**About `INTENT_DECLARED`:** This event records what the **integrator declared** the session was supposed to do, or what was **extracted from the invocation context** if no explicit declaration was supplied. The Governor does not interrogate the agent for its intent, does not infer it from LLM reasoning, and does not verify that the declared objective is correct. The event is a *declaration*, not a *verification*. Its value comes from being comparable against the runtime tool-call events that follow — a reviewer (or the policy engine) can ask *"did the agent's actual behaviour match the declared objective?"* and the trace contains both halves of the answer.

### 4.2 Default policy rules

Every emitted event is evaluated against five rules:

| Rule | What it checks |
| :-- | :-- |
| **POL-001** | Agent must declare intent before executing mutating operations (WRITE / DELETE / EXECUTE) |
| **POL-002** | Agents must be registered before accessing tools |
| **POL-003** | Data entering context must be classified (no empty `data_classifications`) |
| **POL-004** | Memory writes must carry both classification and a retention policy |
| **POL-005** | Sensitive data must not escalate in context without explicit authorization |

When a rule fires, the violating event carries the rule ID in its `policy_violations` list and a plain-language `simulated_consequence` string describing what the paid control-plane would have done. The agent itself is never blocked.

### 4.3 Advisory flags vs policy violations

In addition to the five POL rules, the EventBuilder emits **advisory flags** — softer signals that aren't necessarily violations:

| Advisory flag | Meaning |
| :-- | :-- |
| `AGENT_UNREGISTERED` | Agent's registration metadata is incomplete |
| `INTENT_MISSING` | No intent was declared |
| `SCOPE_OPERATION_UNEXPECTED` | A mutating operation was scoped without intent |
| `SCOPE_INTENT_MISMATCH` | The scoped tool doesn't match the declared intent |
| `CONTEXT_UNCLASSIFIED` | Context arrived without classification metadata |
| `SENSITIVITY_ESCALATION` | Sensitivity tier increased between context snapshots |
| `MEMORY_WRITE_UNCLASSIFIED` | Memory write carries no classification |
| `MEMORY_WRITE_CANDIDATE` | Memory write was inferred from tool name (rather than declared explicitly) |

Advisory flags carry less weight than violations — they're nudges, not assertions of policy breach. The CLI viewer uses `⚠` for any event with either an advisory flag OR a policy violation.

### 4.4 Sessions

A **session** is one logical agent run. Sessions have a lifecycle:

```
IDLE → ACTIVE → CLOSING → CLOSED
```

You enter `ACTIVE` by calling `async with wrapped:` (or by manually invoking `_start()` / `_end()`). The session manager assigns sequence numbers to events, tracks `previous_event_id` chains, and enforces single-active-session-per-agent-id constraints.

Most operators only care that they wrap their tool calls inside an `async with` block. The state machine handles itself.

### 4.5 The classification hook (advanced)

The wrapper's `CONTEXT_SNAPSHOT` event has fields like `data_classifications` and `classification_source` that need to come from somewhere. By default, the wrapper emits empty defaults — which (correctly) triggers `CONTEXT_UNCLASSIFIED` and `POL-003` on every event.

To populate these fields with real values, you supply an optional `classification_hook` callback when you call `wrap_mcp_client`. The hook receives the tool call response and returns a `ClassificationHint` object with whatever fields you can populate. See §8 for examples.

**This is the most important integration point for any non-trivial use of the Governor.** Without it, your traces are technically correct but don't carry meaningful classification metadata.

## 5. Integration: wrapping an MCP-style client

The Governor's wrapper targets an internal `MCPClientLike` protocol with a single method:

```python
def send_tool_call(self, tool_name: str, arguments: dict) -> Any:
    ...
```

To wrap a concrete tool client (any object that knows how to make tool calls), use `SentienceMCPAdapter`:

```python
from sentience_governor.wrapper.mcp import SentienceMCPAdapter, wrap_mcp_client

# Your real client — could be an MCP SDK client, a custom HTTP wrapper,
# whatever you have. It just needs to have SOME way of executing a tool.
class MyToolClient:
    def execute(self, tool_name, params):
        # ... whatever your client does ...
        return {"data": ...}

# Adapt it to MCPClientLike — supply a call_fn that knows how to invoke
# YOUR client's specific method.
adapted = SentienceMCPAdapter(
    delegate=MyToolClient(),
    call_fn=lambda client, name, args: client.execute(name, args),
)

# Now wrap it.
wrapped = wrap_mcp_client(
    target=adapted,
    session_manager=session_manager,
    cache=cache,
    sink_writer=sink,
    agent_id="my-agent-v1",
    agent_version="1.0.0",
    vendor_id="my-company",
    declared_capabilities=["crm.read", "vector_store.write"],
    owner_claim="user_123",
    stated_objective="Generate Q1 report",
)

# Use it as an async context manager.
async with wrapped:
    result = wrapped.send_tool_call("crm.fetch_usage", {"id": "acme"})
```

### What `wrap_mcp_client` accepts

| Parameter | Required | Purpose |
| :-- | :-- | :-- |
| `target` | yes | The `MCPClientLike` (usually a `SentienceMCPAdapter` wrapping your real client) |
| `session_manager` | yes | A shared `SessionManager` instance |
| `cache` | yes | A shared `InProcessCache` instance |
| `sink_writer` | yes | A `SinkWriter` configured with your chosen sink |
| `agent_id` | yes | Stable identifier for this agent (e.g. `"reporting-agent-v1"`) |
| `deployment_mode` | no | `vendor_managed` (default) or `enterprise_managed` |
| `agent_version` | no | Free-form version string (e.g. `"1.0.4"`) |
| `vendor_id` | no | Who built the agent (e.g. `"acme-analytics"`) |
| `declared_capabilities` | no | List of capability strings (e.g. `["crm.read"]`) |
| `owner_claim` | no | The user/principal that owns this session |
| `stated_objective` | no | The declared session objective. Recorded in the `INTENT_DECLARED` event with `intent_source=explicit`. This is **your declaration** at integration time — the Governor records it; it does not validate that the agent will actually obey. |
| `session_id` | no | Override the auto-generated UUID (useful for tests) |
| `classification_hook` | no | See §8 |

Most parameters are optional, but supplying them produces richer traces. The minimum useful invocation needs `target`, `session_manager`, `cache`, `sink_writer`, and `agent_id`.

### Tool name conventions

The wrapper uses the part of the tool name **before the first dot** as the `target_system`. So `crm.fetch_usage` → `crm`; `vector_store.upsert` → `vector_store`. If your tool name has no dot, the entire name is treated as the target system.

### Operation type inference

The wrapper guesses `READ` / `WRITE` / `DELETE` / `EXECUTE` from keywords in the tool name:

- `WRITE` if the name contains `write`, `update`, `create`, `insert`, or `put`
- `DELETE` if it contains `delete`, `remove`, or `drop`
- `EXECUTE` if it contains `exec`, `run`, or `execute`
- `READ` otherwise

If your tool naming doesn't match these heuristics, the wrapper will guess `READ` for everything. There's currently no override; this is tracked as future work.

### Persistence target detection

The wrapper checks the tool name for these keywords to decide whether to emit a `MEMORY_WRITE_ATTEMPT`:

```
{database, vector_store, filesystem, logging}
```

Tools containing any of these keywords trigger a memory write event in addition to the scope/context events. Tools that don't match the keywords don't.

If your persistence target uses a different naming convention (e.g. `report_db.insert`), the wrapper won't detect it. There's currently no override; supplying explicit `persistence_targets` is tracked as future work.

## 6. Integration: LangChain agents

If you're using LangChain, the Governor ships a callback handler:

```python
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import FileSink, SinkWriter
from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler

session_manager = SessionManager()
cache = InProcessCache()
sink = SinkWriter(FileSink("/tmp/langchain-trace.jsonl"))

handler = SentienceCallbackHandler(
    agent_id="my-langchain-agent",
    session_manager=session_manager,
    cache=cache,
    sink_writer=sink,
    agent_version="1.0.0",
    declared_capabilities=["crm.read"],
    owner_claim="user_123",
)

agent.invoke(
    {"input": "Generate Q1 customer report"},
    config={"callbacks": [handler]},
)
```

**Note on `INTENT_DECLARED` from the callback handler.** When the `SentienceCallbackHandler` handles `on_chain_start`, it extracts a string from the chain inputs (looking at keys `input`, `question`, `objective`, `task`, `prompt` in that order) and records it in `INTENT_DECLARED` with `intent_source=inferred`. This is **runtime-extracted**, not integrator-declared — the string came from whoever invoked the chain. Treat it as invocation context (often a user request), not as an authorized objective. The MCP path's `stated_objective` parameter to `wrap_mcp_client` is different: that value is recorded with `intent_source=explicit` because the integrator declared it at wrapper construction time.

The callback handler duck-types the LangChain `BaseCallbackHandler` interface, so there's no hard import-time dependency on `langchain-core`. If you're using `langchain-core`, you can subclass to make the inheritance explicit:

```python
from langchain_core.callbacks import BaseCallbackHandler

class MyHandler(SentienceCallbackHandler, BaseCallbackHandler):
    pass
```

### LangChain middleware (for `create_react_agent` users)

For agents built with `create_react_agent`, there's also a middleware path:

```python
from sentience_governor.wrapper.langchain_adapter import SentienceMiddleware

middleware = SentienceMiddleware(handler)
agent = create_react_agent(llm=llm, tools=tools, prompt=prompt, middleware=[middleware])
```

The middleware is **observe-only in v0** — it never blocks, never modifies tool results, just instruments.

### Sessions, graphs, and concurrency (v0.3.0.2)

One governance session corresponds to one **root invocation**. Frameworks
that fire chain-level callbacks more than once per run — LangGraph fires
them once for the graph and once per node — still produce a single session,
because the handler creates one only for the outermost chain start and tears
it down only when that same run ends. Nested starts and ends are recorded as
structure, not as new sessions.

That matters for what the trace says about you: every session gets its own
`INTENT_DECLARED` baseline, so a tool inside your declared capabilities is
evaluated against that baseline no matter how deeply the graph nests it.
Before v0.3.0.2 a nested run could produce extra sessions with no baseline,
and those reported POL-001 against tools you had in fact declared.

**One handler may be shared across overlapping runs.** Two invocations in
flight at once — on threads or on one event loop — get separate sessions,
and neither can take the other's token usage, model, provider or
`llm_turn_id`. The same isolation holds between parallel branches inside a
single graph, whose LLM turns genuinely overlap.

Two limits worth knowing. `SentienceMiddleware` receives no run identifiers
from LangChain, so it is still **one middleware instance per agent run**; it
attributes tool calls to the single active run and reports a
`GOVERNANCE_ERROR` rather than guessing when more than one is open. And a
tool event that cannot be traced back to a known root is skipped rather than
attached to an arbitrary session.

## 7. Integration: Claude Code sessions

Claude Code is a coding-agent runtime that exposes a hook system on every tool invocation (`Bash`, `Edit`, `Write`, `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, and every `mcp__<server>__<tool>`). The Governor ships an adapter that plugs into that hook system so supported tool calls in a Claude Code session become a governance event — without any code changes to your agent.

**This is the lowest-friction Governor integration.** One command to wire it, one install. No imports, no wrapper construction, no `SessionManager` setup.

### Quick start

```bash
pipx install sentience-governor
sentience init claude-code          # wire hooks and install /sentience-* skills
```

`sentience init claude-code [path]` (v0.2.5.5+) writes — or idempotently merges into — `.claude/settings.json`, resolving the correct absolute path to the hook binary for your install (pipx/pip/source). It never clobbers existing hooks or settings, and re-running is a no-op. Pass a path to target a project other than the current directory.

**It also installs six slash commands (v0.2.8+).** Alongside the hook wiring, `sentience init claude-code` installs `/sentience-help`, `/sentience-pulse`, `/sentience-status`, `/sentience-profile`, `/sentience-violations`, and `/sentience-intent` into `~/.claude/skills/` — so you can read governance signals from the Claude Code chat without switching to a terminal. After install, restart Claude Code and type `/sentience-help`. Each command is a thin wrapper around the corresponding read-only CLI: the CLI produces the deterministic numbers at preprocessing time, and Claude renders them inline and is instructed to show them verbatim. Anything Claude adds beyond the rendered report is interpretation, not Sentience measurement. Note that the boundaries below bind the *commands*, not the agent — Claude retains its normal tool access in the session.

By default, hooks are wired into the initialized project's `.claude/settings.json`, while skills install to your personal `~/.claude/skills/` so the commands are available across Claude Code. Pass `--project` to install the skills into that project's `.claude/skills/` instead.

| Command | What it shows |
|---|---|
| `/sentience-help` | What the commands do and their boundaries |
| `/sentience-pulse` | One-command report for the latest captured session |
| `/sentience-status` | Whether the hook is capturing |
| `/sentience-profile` | Active governance profile, view-only |
| `/sentience-violations` | Per-rule policy-violation drill-down |
| `/sentience-intent` | Per-turn intent-drift drill-down |

The slash surface is intentionally bounded:

- Operator-invoked only: Claude does not run these commands on its own.
- Latest session only: no history browsing, search, aggregate, or session-id selection.
- Read-only: `/sentience-profile` is view-only; no command mutates governance, session, or remote state.

Use `--no-skills` to wire hooks without installing the commands, or `--force` to overwrite a skill you've hand-edited. The install is idempotent: a managed skill is updated on a new release, while a hand-edited one is preserved unless `--force`.

<details>
<summary>Wiring it by hand instead</summary>

Create or edit `.claude/settings.json` in your project (or `~/.claude/settings.json` for user-global). If `sentience-claude-code-hook` isn't on Claude Code's `$PATH`, use its absolute path:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {"type": "command", "command": "sentience-claude-code-hook"}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "",
        "hooks": [
          {"type": "command", "command": "sentience-claude-code-hook"}
        ]
      }
    ],
    "SessionEnd": [
      {
        "matcher": "",
        "hooks": [
          {"type": "command", "command": "sentience-claude-code-hook"}
        ]
      }
    ]
  }
}
```

The `SessionEnd` hook (v0.2.6.1+) is what captures per-turn token burn — see [Per-turn token capture](#per-turn-token-capture-v0261) below. `sentience init claude-code` wires all three events for you.

</details>

Now run Claude Code normally. Supported tool calls produce governance events. By default the hook writes **one file per Claude Code session** under `~/.sentience/traces/claude-code/`:

```
~/.sentience/traces/claude-code/
├── f41ee94f-f686-48c7-8107-8df2749c2a15.jsonl     (this session's trace)
├── f41ee94f-f686-48c7-8107-8df2749c2a15.jsonl.index (sidecar index)
├── a1b2c3d4-....jsonl                              (yesterday's session)
└── a1b2c3d4-....jsonl.index
```

Each file is naturally bounded by the session's own activity — no single trace grows unbounded across weeks of use. Cleaning up is a one-liner:

```bash
# Delete traces older than 30 days
find ~/.sentience/traces/claude-code -name '*.jsonl*' -mtime +30 -delete
```

View the trace with the curated `sentience` CLI:

```bash
sentience status                     # is the hook capturing sessions?
sentience list                       # one line per session, newest first
sentience open --latest              # full curated render of the latest session
sentience open --latest --summary    # same, minus Full Trace — fits one screen
sentience open <session_id>          # render a specific session by id or prefix
```

For raw, full-fidelity event inspection (library traces, golden-trace fixtures, debugging a specific tool call), use `sentience-cli` instead — see §9.

### How chain continuity works across processes

Claude Code spawns a **fresh Python subprocess for every hook invocation**. To keep `event_sequence_number` monotonic and `previous_event_id` chains intact across those restarts, the adapter reconstructs session state from disk on every invocation:

- A sidecar index (`<sink>.index`) records the last event's offset, sequence, and `event_id` per `session_id` for O(1) resumption.
- On every invocation the hook validates the sidecar against the sink; if the sidecar is missing, stale, or corrupt, it falls back to a linear scan and rebuilds.
- An exclusive file lock covers the entire read-plus-append-plus-sidecar-update critical section, so concurrent Claude Code sessions writing to the same sink cannot interleave partial events.

This is invisible at the integrator level. You point Claude Code at the hook; the hook does the rest.

### What to expect in the trace

Claude Code traces are **intentionally high-noise at v0**. You will see:

- `INTENT_MISSING` on every session — Claude Code does not expose the user's prompt to hooks; fabricating intent would be dishonest. Matches the failure-first walkthrough in §3.1.
- `CONTEXT_UNCLASSIFIED` + `POL-003` on every `CONTEXT_SNAPSHOT` — the LangChain / Claude Code paths do not currently support a classification hook. Every tool response arrives unclassified.
- `MEMORY_WRITE_ATTEMPT` with the `MEMORY_WRITE_CANDIDATE` advisory on file-writing tools (`Edit`, `Write`, `NotebookEdit`) and on MCP tools whose names match persistence keywords (`database`, `storage`, `filesystem`, `vector_store`, `logging`).

**v0.2.6.1 does not change any of the above.** `INTENT_MISSING` and `POL-003` are inherent to what Claude Code exposes to hooks, not defects. What v0.2.6.1 adds is per-turn **token-burn attribution**, so `sentience pulse` can rank these signals by how much token/context burn each turn carried — turning high-volume noise into a prioritized "inspect this first" list.

**High advisory volume is expected and intentional.** The Governor's job is to make the gap between "what the agent is doing" and "what the operator wanted" visible. Closing those gaps is integration work; surfacing them is what this adapter provides.

The `sentience` CLI is built around this reality: it **classifies** the advisory volume before it shows you anything. Baseline flags (INTENT_MISSING on every session, POL-003 on every context event) get compressed into a single **Notes** block. Actual anomalies (scope mismatches, unexpected writes, Bash commands that break declared scope) surface in the **Focus** and **Key Events** blocks. See the next section for the full rendering.

### Per-turn token capture (v0.2.6.1)

Through v0.2.6, Claude Code traces recorded *what* the agent did but not *how much token burn each model turn cost* — so `sentience pulse` reported `no_signal` on a Claude Code session. v0.2.6.1 fixes that, and it is why `init claude-code` now wires a third hook, **`SessionEnd`**.

How it works:

- At **`SessionEnd`**, the hook reads Claude Code's session transcript and appends one token-bearing `CONTEXT_SNAPSHOT` per **model turn** (keyed by the transcript's `requestId`), carrying that turn's real token categories: prompt, completion, **cache-read**, and **cache-write**.
- **Cache tokens dominate Claude Code usage.** A turn can show 2 input tokens while reading ~39,000 cached context tokens. Counting only the visible input would understate burn by ~99%, so the capture records the full provider-native breakdown.
- Each live tool call carries the `tool_use_id` Claude Code assigns it. The analyzers attribute a policy violation to its model turn **by that id, not by event position** — so each tool's violation lands on the turn that actually issued it.

What you get:

```bash
sentience pulse --latest        # now shows real per-turn token burn, not no_signal
```

The report distinguishes **total session token burn** from the portion **attributable to a tool-call violation**, and discloses the remainder (reasoning / answer turns) as real burn — "not attributed" never means "no burn." **Token burn** means token / context footprint, **never dollar cost** — there is no rate card or spend estimate.

Honest by construction:

- **Fail-open.** A missing, locked, or partially-written transcript never blocks session end; complete turns are still captured.
- **Idempotent.** Re-running `SessionEnd` does not double-count a turn.
- **Subagent burn is excluded.** Task/Agent subagents have separate transcripts (out of scope this release); when they ran, the report says so rather than presenting partial burn as full-session burn.

If `pulse` still reports `no_token_data` on a Claude Code session, the `SessionEnd` hook is probably not wired — run `sentience init claude-code` and start a new session.

### The `sentience` CLI — reading traces the easy way

The trace file is always canonical JSONL at `~/.sentience/traces/claude-code/<session_id>.jsonl`, but most of the time you want a curated view, not raw events. The `sentience` command gives you three subcommands:

```bash
sentience status                     # verify the hook is working; show the trace path + last session
sentience list                       # one line per session, newest first, max 20
sentience open --latest              # curated render of the most recent session
sentience open --latest --summary    # same as above, skipping the Full Trace block (one screen)
sentience open <session_id>          # render a specific session by id or prefix
```

**`sentience open` produces a six-block output** (seven with the Footer):

| Block | What it shows |
| :-- | :-- |
| **Header** | Session id, timestamp, event count |
| **Summary** | Single-line `Status:` verdict; violation counts (anomalies only); top tools by invocation count |
| **Focus** | The "so what?" block. 1–4 bullets naming specific anomalies in plain English (e.g. "2 write operations outside declared scope"). Sorted by user impact, not policy code |
| **Notes** | Baseline-noise framing. Shown only when baseline patterns are present — skipped entirely for clean sessions |
| **Key Events** | Events with anomalies, max 10, two lines each: the action on line 1, the Issue (code + plain-English gloss) on line 2 |
| **Full Trace** | One line per event. No JSON blobs. Skipped with `--summary` |
| **Footer** | Tips for other sessions + path to raw JSONL for deep dives |

**Baseline-noise classifier.** A `(event_type, code)` pair is treated as baseline only when it matches a known pattern (`INTENT_MISSING` on `INTENT_DECLARED`; `POL-003` / `CONTEXT_UNCLASSIFIED` on `CONTEXT_SNAPSHOT`) **AND** appears in more than 80% of the eligible events of that type in this session. Anything below the threshold, or any other code, surfaces as an anomaly. This protects you from the real signal getting hidden when the adapter's capabilities evolve (a future classification hook would make some context events classified — the remaining unclassified ones would then stop being baseline and correctly show up as anomalies).

**`--summary` flag.** A busy Claude Code session can produce 400+ rendered lines, most of them the Full Trace block. The `--summary` flag skips the Full Trace block while keeping every other block — the result fits on one terminal screen. The JSONL file on disk is untouched; the Footer still shows where to read it. Use `--summary` for interactive review; drop it when you want a full rendered artifact to archive or pipe into a file.

The older `sentience-cli <file>` command is still there and is still the right tool for library traces (MCP wrapper, LangChain) where every event carries unique signal. For Claude Code session traces, `sentience` is what you want.

### Known blind spot — Bash

**Absence of `MEMORY_WRITE_ATTEMPT` on a `Bash` event does NOT imply the command is safe.** The Governor records every `Bash` invocation (tool name, scope, command string in `CONTEXT_SNAPSHOT`) but does not attempt to parse shell-command semantics in v0. A `Bash` call that runs `rm -rf`, `curl | sh`, or `psql ... < migration.sql` will appear as a benign `EXECUTE` + unclassified context without firing a memory-write event. Treat `Bash` scope events as "look here first" when reviewing traces.

### Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
| :-- | :-- | :-- |
| `SENTIENCE_CLAUDE_CODE_SINK_PATH` | `~/.sentience/traces/claude-code/` | Where trace events go. A path ending in `.jsonl` selects **shared-file mode** (every session writes to that single file, interleaved by `session_id`); any other path is treated as a **directory** and the hook writes `<dir>/<session_id>.jsonl` per session (the default). Parent directory is auto-created. |
| `SENTIENCE_CLAUDE_CODE_AGENT_ID_PREFIX` | `claude-code` | Prefix for the derived `agent_id` |
| `SENTIENCE_CLAUDE_CODE_DEPLOYMENT_MODE` | `vendor_managed` | Deployment mode recorded on emitted events |
| `SENTIENCE_CLAUDE_CODE_SIZE_WARN_MB` | `50` | Threshold above which the hook prints a sink-size warning to stderr. Set to `0` to disable. In per-session mode this warning is near-dormant because single-session files rarely cross the threshold. |

### Staying on the single-file layout

Earlier pre-release builds defaulted to a single shared file at `~/.sentience/traces/claude-code.jsonl`. If you relied on that layout — scripts that grep one file, shared viewer sessions, etc. — point the env var at a `.jsonl` path explicitly:

```bash
export SENTIENCE_CLAUDE_CODE_SINK_PATH=~/.sentience/traces/claude-code.jsonl
```

Any path ending in `.jsonl` keeps the hook in shared-file mode with the pre-existing interleaving behaviour. The switch is purely a default change; nothing was removed.

### Fail-open discipline

The hook **never blocks Claude Code tool execution** for any reason. Every error path — malformed JSON on stdin, sink unwritable, lock contention on platforms without `fcntl`, exceptions inside the event builder — is logged to stderr and exits 0. Claude Code treats non-zero exit codes from a `PreToolUse` hook as a reason to abort the tool call; the Governor must never cause that.

### What this integration deliberately does not do

- Does not modify tool inputs or outputs
- Does not block any tool invocation
- Does not send data to any network service (traces are local-file only)
- Does not require an API key or account
- Does not introspect `Bash` command strings for persistence semantics (see blind spot note above)

### Future enhancements

Tracked in the Parking Lot and pulled based on real-world user signal:

- Classification hook on the Claude Code path (today the MCP wrapper supports one; this adapter does not)
- Heuristic `Bash` command classification behind a feature flag
- Lifting the session-resumption primitive into `FileSink` so the MCP wrapper inherits the same continuity guarantees (see §15 "Known limitations")

## 8. Injecting classification metadata via the hook

By default, the wrapper emits `CONTEXT_SNAPSHOT` events with empty classification fields. This is correct (the wrapper has no idea what your tool responses contain) but produces traces where `POL-003` fires on every event. To fix this, supply a `classification_hook` when you call `wrap_mcp_client`.

### The hook signature

```python
from typing import Any, Optional
from sentience_governor.schema.events import ClassificationSource
from sentience_governor.wrapper.mcp import ClassificationHint


def my_classification_hook(
    tool_name: str,
    arguments: dict,
    result: Any,
) -> Optional[ClassificationHint]:
    """Called once per successful tool call.

    Receives the tool name, the arguments the agent passed, and the
    result the tool returned. Should return a ClassificationHint
    describing the classification metadata the wrapper should inject
    into the CONTEXT_SNAPSHOT and (if applicable) MEMORY_WRITE_ATTEMPT
    events. May return None to use wrapper defaults.
    """
    # Example: inspect the result for a conventional metadata key
    if not isinstance(result, dict):
        return None
    metadata = result.get("_metadata") or {}
    if not metadata:
        return None
    return ClassificationHint(
        data_classifications=metadata.get("classifications"),
        classification_source=ClassificationSource.vendor,
        provenance=[tool_name.split(".")[0]],
        retention_flags=metadata.get("retention"),
        context_size_tokens=metadata.get("size_tokens"),
        write_classification=metadata.get("write_classification"),
        retention_requested=metadata.get("retention_requested"),
    )
```

### Wiring it into `wrap_mcp_client`

```python
wrapped = wrap_mcp_client(
    target=adapted,
    session_manager=session_manager,
    cache=cache,
    sink_writer=sink,
    agent_id="my-agent",
    classification_hook=my_classification_hook,  # <-- here
)
```

### What `ClassificationHint` accepts

```python
@dataclass
class ClassificationHint:
    # CONTEXT_SNAPSHOT fields
    data_classifications: Optional[List[str]] = None
    classification_source: Optional[ClassificationSource] = None
    provenance: Optional[List[str]] = None
    retention_flags: Optional[List[str]] = None
    context_size_tokens: Optional[int] = None

    # MEMORY_WRITE_ATTEMPT fields
    write_classification: Optional[str] = None
    retention_requested: Optional[str] = None
```

**Every field is optional and defaults to `None`.** Important rule:

- **`None`** means *"use the wrapper's default for this field"*
- **Any other value** (including `[]`) means *"caller's intentional value, use as-is"*

So if you want to declare *"this tool response has zero classifications, on purpose"*, you must explicitly set `data_classifications=[]`. If you leave it as `None`, the wrapper uses its default (also `[]` today, but conceptually distinct).

### Hook contract guarantees

- The hook is called **exactly once per successful tool call**, with the tool's response as the third argument
- The hook is **NOT called** if the tool itself raised an exception
- If the hook **raises**, the wrapper catches the exception, logs a single bounded warning (tool name + exception class + message — never the result, never the arguments), and falls back to defaults
- The hook **cannot** suppress events, modify tool results, or block agent execution
- The hook receives `result` typed as `Any` — tool results can be any JSON-compatible value (string, list, number, dict, None), not just dicts. Handle accordingly.

### A complete example

See `examples/claude_demo.py` for a working classification hook used in a real Claude tool-use loop. The `extract_classification` function in that script is the canonical reference for how the hook is meant to be written.

### Token tracking (optional, v0.2.3+)

The `ClassificationHint` carries eight optional fields for LLM token accounting. All are `None` by default; populating them lets you correlate token usage with execution behavior (which tools fired, which scope violations triggered, which intents were undeclared) directly in the trace.

| Field | Meaning |
|---|---|
| `llm_prompt_tokens` | Provider-reported input/prompt token count. Cache inclusion or exclusion follows provider semantics — see "Cache semantics" below. |
| `llm_completion_tokens` | Tokens in the model's output |
| `llm_cached_read_tokens` | Tokens read from a prompt cache (Anthropic / OpenAI prompt caching) |
| `llm_cached_write_tokens` | Tokens written to a prompt cache (Anthropic only — OpenAI doesn't separately charge cache writes) |
| `llm_reasoning_tokens` | Hidden reasoning tokens (o1, Claude extended thinking) |
| `model_identifier` | Provider-specific model id (e.g. `"claude-sonnet-4-5"`) |
| `provider` | Provider name (e.g. `"anthropic"`, `"openai"`) |
| `llm_turn_id` | **Dedupe identity** for events sharing the same LLM turn. See "Aggregation warning" below. |

**None vs zero:** `None` means "not reported"; `0` is a real measurement. Fields set to `None` are omitted from the serialized event payload entirely (no schema bloat for non-adopters); fields set to `0` are preserved.

**Anthropic example** (in your classification hook):

```python
from sentience_governor.wrapper.token_extraction import extract_anthropic_usage

def extract_classification(tool_name, arguments, result):
    # ... your existing classification logic ...
    usage_dict = extract_anthropic_usage(your_anthropic_response.usage)
    return ClassificationHint(
        # existing fields ...
        **usage_dict,
        model_identifier=your_anthropic_response.model,
        provider="anthropic",
        llm_turn_id=your_turn_id,  # see "Aggregation warning" below
    )
```

**OpenAI example:**

```python
from sentience_governor.wrapper.token_extraction import extract_openai_usage

usage_dict = extract_openai_usage(openai_response.usage.model_dump())
return ClassificationHint(
    **usage_dict,
    model_identifier=openai_response.model,
    provider="openai",
    llm_turn_id=your_turn_id,
)
```

**LangChain users get token tracking for free.** If you use `SentienceCallbackHandler`, the new `on_llm_start` / `on_llm_end` callbacks capture token usage and attach it to subsequent `on_tool_start` events automatically — no per-hint code needed. LangGraph users with `SentienceMiddleware` should additionally register the new `awrap_step` hook, which aggregates token usage across messages within a step.

#### Cache semantics — no derived math

Different providers count cache differently. **Sentience preserves provider raw values without normalization.**

- **Anthropic:** `input_tokens` *excludes* cache reads. `cache_read_input_tokens` and `cache_creation_input_tokens` are reported separately.
- **OpenAI:** `prompt_tokens` *includes* cached reads. `prompt_tokens_details.cached_tokens` is reported as a sub-count.

Sentience does not subtract cache reads from prompt tokens, recompute totals, or attempt to "normalize" the two providers to a shared semantic. If a provider reports cache fields separately, those fields appear separately in the event. Otherwise they are `None`. **Downstream cost calculation is responsible for understanding the per-provider semantics.**

#### Aggregation warning

When aggregating token usage across multiple `CONTEXT_SNAPSHOT` events (per session, per agent, per time window), consumers MUST dedupe by `(session_id, llm_turn_id)` before summing canonical token fields.

**Why:** when one LLM turn produces several tool calls, every emitted `CONTEXT_SNAPSHOT` from that turn carries the same `llm_turn_id` and the same token usage. This duplication is intentional — it means every event carries the full context of where its tokens came from, even when retrieved out of order. Summing them naively (e.g. `SUM(llm_prompt_tokens) GROUP BY session_id`) inflates totals proportionally to the number of tool calls in each turn.

The correct rollup query shape:

```sql
-- Correct: dedupe by turn first, then sum
SELECT session_id, SUM(llm_prompt_tokens) AS total_prompt_tokens
FROM (
  SELECT DISTINCT ON (session_id, llm_turn_id)
    session_id, llm_turn_id, llm_prompt_tokens
  FROM events
  WHERE event_type = 'CONTEXT_SNAPSHOT'
    AND llm_turn_id IS NOT NULL
) deduped
GROUP BY session_id;
```

**Events with `llm_turn_id IS NULL`** have no framework-provided dedupe identity. Built-in Sentience surfaces emit these only when there's no shared underlying spend (manual hint construction, MCP wrapper passthrough). Custom callers who copy the same token spend across multiple events without populating `llm_turn_id` should provide their own value to enable downstream dedupe.

#### Trace immutability

If `on_tool_start` fires before `on_llm_end` has populated usage for the current turn (rare ordering — streaming LLMs, parallel callbacks), the emitted event includes the current `llm_turn_id` (if `on_llm_start` ran) but NO token fields. **Already-emitted events are NEVER mutated retroactively** when `on_llm_end` later arrives with usage data. This preserves the deterministic-trace contract.

#### Concurrency

The `SentienceCallbackHandler` and `SentienceMiddleware` instance-state pattern assumes **one handler/middleware instance per agent run / session**. Sharing a single instance across concurrent runs would clobber pending-turn state. If your application requires shared instances, switch to `contextvars.ContextVar` — out of scope for v0.2.3, but a known follow-up.

## 9. The `sentience-cli` trace viewer

The package installs a `sentience-cli` command that reads governance traces and renders them as human-readable text.

### Reading a file

```bash
sentience-cli /tmp/my-trace.jsonl
```

### Reading from stdin

```bash
my-agent | sentience-cli
```

### Disabling colour output (for CI logs, snapshot tests, etc.)

```bash
sentience-cli /tmp/my-trace.jsonl --no-colour
```

### What you'll see

For a clean session:

```
SESSION: sess-abc-001 | Agent: my-agent | 2026-04-14T09:00:00Z

[1] REGISTRATION         ✓  Agent my-agent (1.0.0)
[2] INTENT               ✓  Objective: 'Generate Q1 report'  source=explicit
[3] SCOPE                ✓  READ crm.fetch_usage → crm
[4] CONTEXT              ✓  classifications=[internal]  source=vendor  tokens=1200

────────────────────────────────────────────────────────────
SESSION SUMMARY — sess-abc-001

Events intercepted:    4
Policy violations:     0
Actions that would have been blocked:    0
Memory writes that would have been rejected:    0
Intent signal quality: EXPLICIT — intent declared at session start

All events clean — no policy violations.
```

For a session with violations:

```
[4] CONTEXT              ⚠  classifications=[]  source=unclassified  tokens=42
    Policy violation: POL-003
    Consequence: Downstream tool calls requiring classified context would have been restricted.
    → Fix: Vendor should tag tool responses with classification metadata (POL-003)
```

### Input formats accepted

- **Newline-delimited JSON** (one event per line) — the format used by `FileSink` and `HttpLocalSink`
- **JSON arrays** — the format used by the test fixtures (`tests/fixtures/golden_trace_flow_*.json`)

The viewer auto-detects which format you're feeding it.

## 10. Analyzers — derived metrics over captured traces

Once you have governance traces (from any of the integration paths
above), the `analyze` subcommand group computes derived metrics over
them. Analyzers are pure-function modules: no I/O, no environment
reads, no input mutation, byte-stable output. They never modify your
trace files.

### 10.1 `sentience analyze undeclared-intent` (v0.2.4)

Computes how much of a session's compute was attributed to reasoning
turns that touched execution outside the session's declared
operational intent. Available since v0.2.4.

```bash
# Most recent captured session
sentience analyze undeclared-intent --latest

# Specific session by id prefix (matches files under
# ~/.sentience/traces/claude-code/)
sentience analyze undeclared-intent 7f3b

# Any NDJSON trace file directly
sentience analyze undeclared-intent /path/to/session.jsonl

# Structured JSON output (no save prompt, scriptable)
sentience analyze undeclared-intent --latest --json

# Skip the prompt and save the Markdown report directly
sentience analyze undeclared-intent --latest --save

# Disable the post-render save prompt entirely
sentience analyze undeclared-intent --latest --no-prompt
```

Sample output (status `ok`, declared intent present, one drift turn):

```
Undeclared-Intent Spend — session demo-v02...
─────────────────────────────────────────────
Total compute           4,840 tokens
Undeclared              1,000 tokens   (20.7%)
Declared                3,840 tokens   (79.3%)

Undeclared turns
  Turn turn-3    slack.write_message     1,000 tokens   INTENT_MISSING,POL-001

1,000 tokens were attributed to turns that touched execution outside
this session's declared operational intent. Were these valid and
expected? If not, policy can intervene at the execution boundary —
review, constraint, confirmation, or block.

Save this report? [Y/n]:
```

### 10.2 What the metric means

For each reasoning turn, the analyzer asks: did any tool call in
this turn fire `INTENT_MISSING` (advisory) or `POL-001` (policy
violation — write op without declared intent)? If yes, every token
spent on that turn counts as **undeclared spend**. The conservative
rule applies: a single off-task call marks the whole turn.

The headline percentage is `undeclared_tokens / total_tokens`. The
per-turn list grounds the number; the footer paragraph translates
the result into operational language.

### 10.3 Status branches

| Status | Meaning |
|---|---|
| `ok` | Full attribution; no warnings accumulated. |
| `partial` | Analysis completed but warnings accumulated (unpaired events, untokened pairs, dedupe conflicts, or malformed events). Numeric attribution is correct on the readable subset. |
| `no_token_data` | No `CONTEXT_SNAPSHOT` events with populated `llm_turn_id` were found. Usually means token-attribution hooks are not wired (the MCP wrapper auto-emits them). **On Claude Code:** run `sentience init claude-code` to wire the `SessionEnd` hook, then start a new session. |
| `no_turns` | Reasoning turns were detected but none carried populated token totals. |

### 10.4 Differentiated footer copy

The footer line shifts based on whether any `INTENT_DECLARED` event
fired in the session:

* **Agent-bound** (intent was declared but the agent acted outside
  it): "*N tokens were attributed to turns that touched execution
  outside this session's declared operational intent…*"
* **Surface-bound** (no intent declaration anywhere): "*This
  session contains no declared intent — every attributed turn is
  classified as undeclared. Often this reflects a surface-level
  limitation (e.g. Claude Code hooks today, which don't yet expose
  an intent-declaration primitive) rather than agent drift.*"

The framing is deliberate. When the surface itself does not provide
an intent primitive, the metric correctly diagnoses an
ecosystem-level absence rather than blaming the agent.

### 10.5 Saved Markdown report

Pressing **Y** at the save prompt (or passing `--save`) writes a
Markdown report to:

```
~/.sentience/reports/undeclared-intent-<sid-prefix>-<timestamp>.md
```

The report contains the headline metric, the per-turn breakdown, the
operational interpretation paragraph, and a two-vector footer
(direct reply path + launch-list link). The save flow is suppressed
for non-`ok` status — the analyzer never prompts to save a degraded
result.

### 10.6 JSON output schema

Stable across the v0.2.x line. Pass `--json` to receive:

```json
{
  "session_id": "7f3b...",
  "status": "ok",
  "session_has_declared_intent": true,
  "total_tokens": 12847,
  "undeclared_tokens": 4892,
  "declared_tokens": 7955,
  "undeclared_ratio": 0.381,
  "undeclared_percent": 38.1,
  "undeclared_turn_count": 3,
  "total_turn_count": 12,
  "undeclared_turns": [
    {
      "turn_id": "9b3a...",
      "tokens": 2140,
      "reasons": ["POL-001"],
      "tool_ids": ["crm.list_invoices"]
    }
  ],
  "warnings": [],
  "unpaired_event_count": 0,
  "untokened_pair_count": 0,
  "dedupe_conflict_count": 0,
  "malformed_event_count": 0
}
```

### 10.7 Showcase examples

Three deliberate scenarios are pre-rendered under
`examples/showcase/` (in the source repo):

* `sample_report_low_undeclared.md` — agent mostly on-task (~10%
  undeclared)
* `sample_report_high_undeclared.md` — agent drifts heavily (~50%
  undeclared)
* `sample_report_no_intent.md` — surface-bound footer (Claude Code
  case where no intent primitive exists)

A runnable end-to-end demo lives at
`examples/v024_undeclared_intent_demo.py`.

### 10.8 Pure-function guarantees

The analyzer module
(`sentience_governor.analyze.undeclared_intent`) holds four
load-bearing properties:

* No I/O. No file reads, no network, no logging side effects.
* No environment state read. No `os.environ`, no time-based
  branches, no random sources.
* No input mutation. Read-only access patterns; defensive copies
  where needed.
* Byte-stable output for identical inputs. `repr(result_a) ==
  repr(result_b)` holds across runs.

These guarantees enable golden-trace tests, replay, and snapshot
comparison without re-validating the analyzer each time. File I/O
(reading a trace, writing the saved report) lives in the CLI
handler, not the analyzer.

---

## 11. Governance Profiles

A **governance profile** is an operator-authored YAML file at
`~/.sentience/profile.yaml` that encodes what you expect of any
agent on this machine. It's a sibling artifact to the agent's
`CLAUDE.md` recipe — the recipe is the human surface, the profile
is the machine surface, both encoding the same expectations.

Sessions that run without a profile produce traces byte-identical
to v0.2.4. The profile system is strictly additive: every existing
v0.2.4 integration continues to work unchanged.

### 11.1 What a profile shapes

In v0.2.5 a profile shapes three things, all observability signals
(not enforcement):

1. **When undeclared intent is surfaced** (`session_intent.demand_at`).
   Three values:
   - `session_start` — every event without intent fires `POL-001`
     (v0.2.4-compatible default).
   - `first_write` — `POL-001` fires once per session on the first
     mutating operation. The recommended setting for noisy traces.
   - `never` — `POL-001` suppressed entirely; the underlying
     advisory flag stays.

2. **When the agent has crossed a task boundary**
   (`task_boundary.signals`). Four candidate signals:
   - `dir_change` — directory shifted at the configured depth.
   - `file_type_shift` — file extension changed between events.
   - `read_to_write_transition` — agent moved from `READ` to a
     mutating operation.
   - `time_gap` — gap since the previous tool call exceeded
     `time_gap_seconds`.

   Any active signal that fires adds `TASK_BOUNDARY_CROSSED` to
   the next `SCOPE_ASSERTED` event's `advisory_flags`. Multiple
   signals on one event produce a single flag.

3. **Which tools should be treated as high-consequence**
   (`high_consequence.tools`). A list of regex patterns matched
   against the composite `<tool_id>:<target_system>` (so
   `Bash:rm -rf /tmp/scratch` matches `Bash:.*rm.*-rf.*`). A
   match adds `HIGH_CONSEQUENCE_DETECTED` to that event.

The runtime is strictly observational. Flags appear in the trace
and in analyzer output; nothing is blocked, scoped, or modified.

### 11.2 Creating your first profile

Three commands get you from no-profile to a working starter:

```bash
sentience profile init       # create ~/.sentience/profile.yaml with defaults
sentience profile view       # inspect what was written
sentience profile validate   # schema check (READ-ONLY; never mutates the file)
```

`init` refuses to overwrite an existing profile — if one is
already in place, edit it instead:

```bash
sentience profile edit       # opens an editor on ~/.sentience/profile.yaml
                             # ($VISUAL → $EDITOR → nano/vim/vi → macOS TextEdit)
```

That's the entire setup. Once the file exists, every governed
session — Claude Code hook, MCP wrapper, LangChain handler —
picks it up automatically at session start. No environment
variable, no constructor argument, no flag.

### 11.3 Profile schema

The file format is YAML, with three top-level sections plus a
schema version header. Comments are preserved; the operator's
hand-authored file is never rewritten by the runtime.

```yaml
# Schema version: 1
# Content hash: sha256:<auto-computed>
# Generated: 2026-05-13T10:00:00Z

schema_version: 1

session_intent:
  required: true
  demand_at: first_write     # session_start | first_write | never

task_boundary:
  signals:
    - dir_change
    - file_type_shift
    - read_to_write_transition
  time_gap_seconds: 300
  dir_change_depth: 2

high_consequence:
  tools:
    - "Bash:.*rm.*-rf.*"
    - "Bash:.*git.*push.*--force.*"
    - "fs.write:.*\\.env.*"
```

**Defaults when fields are omitted:**

| Field | Default | Meaning |
|---|---|---|
| `session_intent.required` | `true` | A session ending with no `INTENT_DECLARED` is flagged. |
| `session_intent.demand_at` | `session_start` | Preserves v0.2.4 behavior. |
| `task_boundary.signals` | `[]` (empty) | No boundary detection; `TASK_BOUNDARY_CROSSED` never fires. |
| `task_boundary.time_gap_seconds` | `300` | Used only if `time_gap` is in `signals`. |
| `task_boundary.dir_change_depth` | `2` | Used only if `dir_change` is in `signals`. |
| `high_consequence.tools` | `[]` (empty) | No tool patterns; `HIGH_CONSEQUENCE_DETECTED` never fires. |

**Reserved sections** (recognized but ignored in v0.2.5; reserved
for future composition features):

- `extends` — profile inheritance / composition.
- `policies` — per-rule toggle and tuning (e.g. disable POL-003 for read-heavy agents).
- `custom_rules` — operator-defined match rules with custom advisory flags.

The runtime accepts these fields without error; they're carried
through validation as warnings. Future releases will activate them
without changing `schema_version`.

**Header semantics.** The three header lines (`Schema version`,
`Content hash`, `Generated`) are advisory. `sentience profile
validate` recomputes the content hash and reports whether it matches
the header — but it never modifies the file. If you've edited the
profile by hand, the hash will drift; re-running `sentience profile
export ~/.sentience/profile.yaml` rewrites the header against the
current contents.

### 11.4 CLI commands

Six verbs under `sentience profile`:

| Verb | What it does |
|---|---|
| `init` | Create a starter profile at `~/.sentience/profile.yaml`, with inline explanatory comments on every field. Refuses to overwrite an existing file. |
| `view` | Print the active profile. Shows defaults when no file exists, with a banner. |
| `validate [path]` | Schema check against the profile (defaults to `~/.sentience/profile.yaml`). **Read-only** — never mutates the file. Reports content-hash integrity against the header; if you've edited the file, it shows an informational note that the header hash is stale (the runtime uses the recomputed hash), not an error. Exits non-zero on schema errors. |
| `export <path>` | Write the active profile to an explicit path with a fresh header (recomputed content hash + timestamp). Inline field comments are included. |
| `import <path>` | Read a profile from an explicit path, validate it, and install at `~/.sentience/profile.yaml`. Refuses to install a profile that fails validation. |
| `edit` | Open `~/.sentience/profile.yaml` in an editor. Resolves `$VISUAL` → `$EDITOR` → `nano`/`vim`/`vi` → (macOS) `open -e`; errors only if no file exists (run `init` first) or no editor can be found. |

`validate` has two flags worth knowing:

- `--strict` — error on unknown top-level keys instead of warning. For CI / production environments where profile drift should fail loudly.
- (positional `path`) — validate a profile at an arbitrary path rather than the default. Useful for pre-flight checking a profile someone shared with you before installing it.

### 11.5 What firing looks like in the trace

Every event from a profile-governed session carries an envelope-level
`profile_fingerprint` — the first 12 hex characters of the profile's
canonical content hash. The fingerprint stays constant across the
session's lifetime; mid-session changes to the profile file do not
affect any already-active session.

`AGENT_REGISTERED` additionally carries two payload-level fields:

```json
{
  "profile_loaded": true,
  "profile_schema_version": 1
}
```

When no profile file exists, all three fields are absent from the
serialized JSON — v0.2.4-shaped traces stay byte-identical under
v0.2.5.

The two new advisory flag values:

- `TASK_BOUNDARY_CROSSED` — fires on `SCOPE_ASSERTED` events when any
  configured `task_boundary.signals` triggers against the previous
  event's state.
- `HIGH_CONSEQUENCE_DETECTED` — fires on `SCOPE_ASSERTED` events when
  the `<tool_id>:<target_system>` composite matches any pattern in
  `high_consequence.tools`.

Both flags appear in `advisory_flags` lists alongside existing flags
(`INTENT_MISSING`, `SCOPE_OPERATION_UNEXPECTED`, etc.). Analyzers that
don't recognize the new values treat them as unknown strings and
ignore them — the schema is forward-compatible.

**Analyzer extensions.** `sentience analyze undeclared-intent`
gains three optional sections in both CLI and Markdown output:

- A **Profile** section showing the fingerprint and schema version
  (correlates the report to the profile that produced it).
- A **High-consequence operations** table listing every event where
  `HIGH_CONSEQUENCE_DETECTED` fired (with turn and tool).
- A **Task boundaries crossed** table listing every event where
  `TASK_BOUNDARY_CROSSED` fired.

Each section is omitted when its underlying field is absent or
empty, so analyzer output on v0.2.4-shaped traces is byte-identical.
v0.2.4 traces don't lose detail; v0.2.5 traces gain it.

### 11.6 Closed-loop walkthrough

A complete runnable embodiment of the profile loop lives at
`examples/showcase/v025-closed-loop/`:

| File | Role |
|---|---|
| `profile.yaml` | Operator-authored governance profile (the same one you'd drop at `~/.sentience/profile.yaml`). |
| `CLAUDE.md` | Recipe template the operator places in their project so the agent knows what's expected of it. |
| `session.jsonl` | Synthesized governed session trace (pinned; regenerated by the demo script). |
| `analyzer_output.md` | Pre-rendered Markdown report showing the three new sections firing. |
| `README.md` | Walkthrough explaining each piece and how they connect. |

Run it end-to-end:

```bash
python examples/v025_closed_loop_demo.py
```

The script loads the profile, builds a synthesized trace (deterministic
event IDs, fixed timestamp), runs the analyzer, and prints both the
CLI render and a pointer to the Markdown report. Byte-stable: the
same inputs produce identical outputs across runs, so the trace and
report files can live alongside the script as fixtures.

Re-analyze the same trace via the CLI to verify the analyzer
behavior matches:

```bash
sentience analyze undeclared-intent examples/showcase/v025-closed-loop/session.jsonl
```

You'll see the Profile section, the High-consequence operations
table, and the Task boundaries crossed table — exactly the
artifacts the demo produces, exactly the artifacts a real governed
session would surface.

### 11.7 LangChain integration

The LangChain handler — `SentienceCallbackHandler` and
`SentienceMiddleware` — picks up the profile transparently. At
session start (`on_chain_start`), the handler calls
`GovernanceProfile.from_default_path_or_none()`; if a profile file
exists, the session runs governed by it. No keyword arguments
change; no constructor flags need adjusting.

Mapping your existing setup to profiles:

- The handler's `stated_objective` constructor argument (when
  supplied) populates the `INTENT_DECLARED` event the way it always
  has. The profile's `session_intent.demand_at` value decides what
  the wrapper does when a session ends without one.
- The handler's `agent_id` / `vendor_id` constructor arguments are
  unchanged. They surface on the `AGENT_REGISTERED` event, alongside
  the new `profile_loaded` and `profile_schema_version` fields when
  a profile is active.
- LangChain tools execute through the callback handler's tool
  hooks. Each tool's `SCOPE_ASSERTED` event is where the v0.2.5
  advisory flags fire — `TASK_BOUNDARY_CROSSED` when a signal
  triggers, `HIGH_CONSEQUENCE_DETECTED` when a regex matches against
  `<tool_id>:<target_system>`. Existing flags (`INTENT_MISSING`,
  `SCOPE_OPERATION_UNEXPECTED`) continue to fire as in v0.2.4.

Sessions still emit the same primitive events; only the
advisory_flags / policy_violations / envelope `profile_fingerprint`
fields change shape. Existing analyzers that don't recognize the new
flags continue to work — they list the values as unknown strings and
ignore them.

### 11.8 MCP integration

`wrap_mcp_client` picks up the profile the same way. At
`_WrappedMCPSession._start()` the wrapper calls
`GovernanceProfile.from_default_path_or_none()` and passes the
result to `SessionManager.session_start(profile=...)`. The
`wrap_mcp_client` signature is unchanged — no new keyword arguments.

Mapping your existing setup:

- The `stated_objective` kwarg on `wrap_mcp_client` continues to
  emit `INTENT_DECLARED` at session start. The profile's
  `session_intent.demand_at` decides POL-001 firing behavior for
  sessions that never declare intent.
- The `classification_hook` you pass to `wrap_mcp_client` is
  unaffected — classification metadata flows through the same
  `CONTEXT_SNAPSHOT` and `MEMORY_WRITE_ATTEMPT` events as before.
- Tool patterns in `high_consequence.tools` are matched against the
  composite `<tool_id>:<target_system>`. For MCP-style tools,
  `tool_id` is the MCP tool name and `target_system` is whatever
  the wrapper inferred from the call arguments (commonly a path,
  table name, or domain identifier). Test your regexes against a
  representative trace before relying on them in production.

The Claude Code hook (`sentience_governor.wrapper.claude_code_hook`)
uses the same loader path and is wired identically.

---

## 12. Sentience Pulse

**New in v0.2.6.** `sentience pulse` is a single command that
composes the v0.2.4 undeclared-intent analyzer, the v0.2.6 policy-
violation burn-rate analyzer, and an advisory-flag-occurrence
summary into one shareable session report. It is the v0.2.6
adoption surface — the command operators run after every session.

**Claude Code (v0.2.6.1+):** `sentience init claude-code` now also wires
the `SessionEnd` hook, so a live Claude Code session carries per-turn
token-burn attribution and `pulse` no longer reports `no_signal`. If
`pulse` still shows `no_token_data` on a Claude Code session, rerun
`sentience init claude-code` and start a new session.

```bash
sentience pulse --latest           # most recent session
sentience pulse 7f3b               # session-id prefix
sentience pulse /path/to/session.jsonl    # explicit trace file
sentience pulse --showcase         # bundled clean-session example
sentience pulse --latest --json    # structured JSON output
sentience pulse --latest --save    # write Markdown report, no prompt
sentience pulse --latest --no-prompt    # disable interactive save prompt
```

### 12.1 What pulse composes

| Section | Source | What it tells you |
|---|---|---|
| Undeclared-intent spend | v0.2.4 `compute_undeclared_intent_spend` | How much compute attached to turns without declared intent. |
| Policy-violation burn rate | v0.2.6 `compute_policy_violation_burn_rate` | Per-rule attribution of compute on turns where POL-001 through POL-005 fired. |
| Advisory flags | event-envelope `advisory_flags` summary | Per-flag occurrence counts across the trace (all ten flags shipped in the v0.2.5 schema). |
| Profile context | event-envelope profile fields | Whether a profile was loaded and which fingerprint pinned the trace. |
| Interpretation block | clean-session path only | "No policy violations recorded against the rules active in this session…" — the recurring-loop value statement for fresh-operator first sessions. |

Each section ends with a one-line **"Why it matters"** translation —
the metric becomes operator action, not raw data. The five-section
layout is canonical: pulse always renders the sections in the same
order so the report is recognisable run-to-run.

### 12.2 Status branches

Pulse normalizes each sub-analyzer's raw status to one of five
categories before merging. The merged pulse status is one of:

| Pulse status | Meaning |
|---|---|
| `ok` | At least one analyzer produced usable signal; no warnings. |
| `partial` | At least one analyzer accumulated warnings. Numeric attribution is correct on the readable subset. |
| `limited` | Some analyzers had usable data; others didn't (e.g. token data missing for one analyzer). Pulse prepends a "Limited signal — N of M analyzers had usable data" notice. |
| `no_signal` | No analyzer produced usable signal — typically a trace with no token data and no reasoning turns. The pulse still renders a brief honest framing. |

**All pulse statuses are save-eligible.** This is a deliberate
divergence from the standalone analyzer commands. A `no_signal`
pulse is itself a useful artifact — it tells the reader the trace
had no usable analyzer signal, which is information. Pulse
aggregates across analyzers and ships an Interpretation block on
every path, so every pulse output is worth saving.

**Token-class breakdown and the live pulse (v0.2.8.2).** On an
`ok`/`partial` pulse, the undeclared-intent section breaks total
compute into the four token classes — cached read / cached write /
prompt / completion — which reconcile exactly to the total (cache
reads typically dominate a Claude Code session, often well over 90%).
A methodology line states that per-turn usage is deduped by
`requestId`, so a cache-read total is not misread as cumulative. On an
empty latest session (`no_signal`), the pulse **shows your most recent
session that *does* have token data**, with a transparent header naming
it — instead of an empty report. An explicit `sentience pulse <id>` is
honoured exactly.
(Useful because resuming a Claude Code conversation mints a new
session id, so the newest session is often an empty live segment.)

### 12.3 Policy-violation burn rate

The burn-rate analyzer (also available standalone as `sentience
analyze policy-violations`) aggregates per-rule compute associated
with turns where one or more policy rules fired. Rule descriptions
shipped with v0.2.6:

| Rule | What it surfaces |
|---|---|
| `POL-001` | Intent not declared before mutating operation. |
| `POL-002` | Agent registration missing key signals. |
| `POL-003` | Context snapshot carried no classification metadata. |
| `POL-004` | Memory write attempt lacked classification or retention policy. |
| `POL-005` | Sensitivity escalated without explicit authorization. |

**Attribution discipline (load-bearing).** Burn rate measures
compute *associated with* turns where rules fired. It does not
prove the violation caused the compute spend. The metric is a
deterministic prioritization signal for operator inspection — the
rule with the most associated compute is the first one to inspect.
Copy throughout the analyzer uses association language only ("appeared
on turns representing N tokens", "good place to inspect first"). It
never uses savings or causality wording ("would reclaim", "could
save", "would prevent", "would have been").

**Non-additivity.** If a turn fires multiple rules, that turn's
tokens appear under each fired rule in `by_rule.X.token_cost`. Summing
the per-rule rows can therefore exceed the top-level
`violation_associated_tokens` total — by design. Three fields preserve
trust:

* `total_tokens` — session compute total.
* `violation_associated_tokens` — sum across unique violation-firing
  turns (deduplicated; bounded by `total_tokens`).
* `by_rule.X.token_cost` — per-rule, with overlap allowed; NOT
  additive across rules.

The non-additivity callout fires inline between the by-rule rows
and the "Why it matters" line whenever more than one rule has
non-zero token cost — operators see it the moment they read the
per-rule table.

### 12.4 Email-list footer

The pulse Markdown footer includes a one-line **email-list** CTA:
*"Want this pulse delivered weekly via email? Join the list."* (This
is the "Sentience Sync" email list at `getsentience.ai/sentience-sync`
— unrelated to the cloud telemetry that was sunset in v0.2.8.3.)
Eligibility:

* Not subscribed — no `~/.sentience/first-run.json` file, or
  `subscribed` is not set → footer **shows** with reason
  `not_subscribed`.
* `subscribed` is truthy → footer **suppressed**, reason
  `already_subscribed`.
* `SENTIENCE_NO_SYNC_PROMPT=1` env var set → footer **suppressed**,
  reason `opted_out` (precedence over subscription state).

Eligibility lives in the CLI handler; the `compute_pulse` analyzer
is pure (no disk reads, no env reads). The `--no-prompt` flag
suppresses the interactive save prompt only — it does NOT suppress
the sync footer (the footer is non-interactive Markdown, not a
prompt).

### 12.5 Saved Markdown report

Pressing **Y** at the save prompt (or passing `--save`) writes:

```
~/.sentience/reports/pulse-<sid-prefix>-<timestamp>.md
```

Same template across statuses. The Markdown report is designed to
be standalone-understandable: paste it into a GitHub issue, a Slack
message, an advisor update, or a customer / investor proof point
without context from the operator who ran the session. The "Why it
matters" line in each section is what makes the report readable
cold.

### 12.6 JSON output schema

Stable for the v0.2.6 line. Pass `--json` to receive the full
composed dict:

```json
{
  "schema_version": 1,
  "analyzer": "pulse",
  "analyzer_version": "0.2.6",
  "session_id": "7f3b...",
  "status": "ok",
  "session_summary": {
    "total_events": 142,
    "total_turns": 8,
    "session_duration_seconds": 487
  },
  "undeclared_intent": { /* full v0.2.4 result dict */ },
  "policy_violations_burn_rate": { /* full v0.2.6 result dict */ },
  "advisory_flag_summary": {
    "TASK_BOUNDARY_CROSSED": 3,
    "HIGH_CONSEQUENCE_DETECTED": 1,
    "INTENT_MISSING": 0,
    "AGENT_UNREGISTERED": 0,
    "SCOPE_OPERATION_UNEXPECTED": 0,
    "SCOPE_INTENT_MISMATCH": 0,
    "CONTEXT_UNCLASSIFIED": 0,
    "SENSITIVITY_ESCALATION": 0,
    "MEMORY_WRITE_UNCLASSIFIED": 0,
    "MEMORY_WRITE_CANDIDATE": 0
  },
  "sync_prompt": {
    "show": true,
    "reason": "not_subscribed"
  },
  "profile_fingerprint": "a1b2c3d4e5f6",
  "profile_loaded": true,
  "profile_schema_version": 1
}
```

The `advisory_flag_summary` dict always carries all ten
`AdvisoryFlag` enum members so downstream code can index any flag
without `KeyError`.

### 12.7 Showcase examples

Three deliberate scenarios are pre-rendered under
`examples/showcase/v026-pulse/` in the source repo:

* `clean/` — well-behaved agent + permissive profile → no
  violations, full per-turn attribution. The most-common first-
  session outcome for fresh operators.
* `missing_intent/` — Claude Code-style trace where the runtime
  doesn't expose intent → POL-001 on every mutating turn, surface-
  bound framing in the undeclared-intent section.
* `mixed_violations/` — tighter profile, multiple POL rules firing
  across multiple turns. The per-rule prioritization signal
  becomes visible.

A runnable end-to-end demo lives at
`examples/v026_pulse_demo.py`. The v0.2.5 closed-loop showcase has
a `pulse_output.md` cross-link that complements the `clean/`
sub-case.

### 12.8 Pure-function guarantees

`compute_pulse` (in `sentience_governor.analyze.pulse`) holds the
same four load-bearing properties as the v0.2.4 analyzer:

* No I/O. No file reads, no network, no logging side effects.
* No environment state read. No `os.environ`, no time-based
  branches, no random sources.
* No input mutation. The input event list is treated as read-only.
* Byte-stable output for identical inputs.

Sync-prompt eligibility (the one place disk + env reads happen)
lives in the CLI handler in `sentience_governor.cli.ux`, NOT in
the analyzer. The renderers in `sentience_governor.analyze.renderers`
are pure too — they read `result["sync_prompt"]["show"]` to decide
whether to render the footer, and never touch disk or env directly.

### 12.9 Tool calls and tool-token attribution (v0.2.9)

The pulse surfaces two tool-activity facts, both derived from events
already captured since v0.2.6.1 (no `schema_version` bump):

* **Tool calls (F21).** Every `SCOPE_ASSERTED` is one tool call. The
  pulse reports the total, the four operation classes (execute / read /
  write / delete), and the top tools by call count. The
  `undeclared_intent.tool_calls` block carries `total`, `by_operation`
  (which sums to `total` for well-formed traces), and `by_tool`.
* **Tool-token attribution (IR-3).** The pulse reports the tokens on
  turns that fired at least one tool call (the headline) and a per-tool
  view. **Attribution stops at the turn.** The model meters tokens per
  turn, not per tool, so a per-tool token *cost* is not measurable: the
  pulse says "tokens on turns involving tool X," never "tool X spent N."
  The per-tool view is **full-turn-credit and non-additive**: a turn that
  fires several tools credits each with the full turn total, so the
  per-tool numbers can sum to more than the headline. The
  `undeclared_intent.tool_token_attribution` block carries
  `tokens_on_turns_with_tool_calls`, `total_tokens`, `percent_of_total`,
  and a `by_tool` list of `{tool_id, tokens, turn_count}` with
  `by_tool_is_non_additive: true`. Without the `tool_use_id` →
  `llm_turn_id` join (older positional traces), these report `0`, never
  an inferred split.

### 12.10 `sentience explain`: how the numbers are counted (v0.2.9)

`sentience explain` is the methodology surface: it states, deterministically,
*how Sentience counts*, independent of any session.

```bash
sentience explain          # human-readable
sentience explain --json   # the same methodology as structured JSON
```

It covers the four token classes, the dedupe-by-`llm_turn_id` rule
(`llm_turn_id` is the model-invocation / requestId boundary), the
per-turn (not per-tool) attribution boundary, the `operation_type` enum
(READ / WRITE / DELETE / EXECUTE), and the `tool_use_id` → `llm_turn_id`
join semantics. It is methodology-only in v0.2.9 (no per-code mode), and
the `--json` form is what the MCP adapter consumes. The methodology lives
in `sentience_governor.analyze.methodology` as a single source of truth.

### 12.11 The MCP server: governance Claude can call (v0.3.0)

v0.3.0 adds an **opt-in MCP server** (`sentience-mcp-server`) so Claude can
call Sentience governance tools directly inside a session. It is **stdio,
local, no HTTP, no auth**, and **never registered by default**.

Install the server dependency and register it per project:

```bash
# virtualenv / pip
pip install "sentience-governor[mcp]"

# pipx-managed install (ambient pip cannot reach the pipx venv)
pipx install --force "sentience-governor[mcp]"

sentience init claude-code --mcp     # writes .mcp.json + shows a consent notice
```

The `--mcp` flag is additive: plain `sentience init claude-code` is
unchanged and registers nothing MCP-related. Registration writes a
`sentience` entry into the project's `.mcp.json` and prints a consent
notice stating what the tools can and cannot do.

**The seven tools:**

| Tool | Kind | What it returns |
| :-- | :-- | :-- |
| `sentience_explain` | session-independent read | the §12.10 methodology |
| `sentience_profile_view` | session-independent read | your declared governance posture (authoritative, not inferred) |
| `sentience_pulse` | last-completed-session read | the full pulse of the last completed session |
| `sentience_intent` | last-completed-session read | undeclared-intent spend of the last completed session |
| `sentience_violations` | last-completed-session read | policy-violation burn rate of the last completed session |
| `sentience_session_status` | current-session read | structural status of the live session |
| `sentience_declare_intent` | current-session write | records the agent's stated intent for this session |

**Why the reads split by session.** Token analysis is only available after
a session ends (SessionEnd), so the measured reads (`pulse` / `intent` /
`violations`) operate on the **last completed** session and each **names
the session it read**, so a completed-session reading is never mistaken for
the live one. `sentience_session_status` is the only live-session read and
is **structural-only**: event count, tool-call counts by operation class,
and policy / advisory counts so far. It never returns a token, burn, or
pulse figure, and says so (`token_analysis: "unavailable until
SessionEnd"`).

**`sentience_declare_intent(objective, scope)`** is the one forward-looking
write. The agent states the objective it is working toward and the
operation targets that objective authorizes (its `scope`, e.g.
`["filesystem"]`). Sentience records it as a **server-written, append-only**
`INTENT_DECLARED` event on the current session's trace; from then on,
matching activity stops firing POL-001 **at capture**, while
pre-declaration events keep their POL-001 (**non-retroactive** — the
declaration is never applied backwards). It **fails closed** on any
uncertain session binding and writes nothing.

**What `intent_source = inferred` means here.** A `declare_intent` event is
recorded as `intent_source = inferred`, `intent_confidence = inferred_low`.
This is deliberate and honest: the declaration is **agent-declared through
the MCP channel**, so the *mechanism* is reliable, but the *content* is
**untrusted and NOT integrator-vouched**. It is not the same as an
integrator declaring an objective at construction time (that is
`explicit`). Read an `inferred` declaration as "the agent said this," never
as an operator endorsement or an authorized objective.

**See the flip without the server.** `sentience demo declare-intent` prints
a BEFORE/AFTER showcase of the POL-001 flip a mid-session declaration
produces (100% undeclared compute becomes 37.5% once the declaration lands,
pre-declaration turn unchanged). It is a **deterministic, self-contained
synthetic showcase**: it builds sessions through the real capture-time
evaluator to prove the flip is genuine, but it does **not** spawn the MCP
server or exercise live session identification. Use it to understand the
mechanism, not as a test of your live MCP wiring.

---

## 13. Sinks: where governance events go

The package ships three sink implementations. You wrap whichever one you choose in a `SinkWriter` and pass it to `wrap_mcp_client`.

| Sink | Use when |
| :-- | :-- |
| `StdoutSink` | You want events on stdout (default; pipe into `sentience-cli`) |
| `FileSink(path)` | You want events appended to a file (newline-delimited JSON) |
| `HttpLocalSink(url)` | You want events POSTed to a local HTTP endpoint (loopback only, 500ms timeout) |

Example:

```python
from sentience_governor.sink.writer import FileSink, SinkWriter

sink = SinkWriter(FileSink("/var/log/sentience/agent.jsonl"))
```

### Sink failure semantics

All sinks share the same failure handling:

- Writes are **synchronous** and **unbuffered**. If the sink is slow, the agent waits.
- On failure, the event is **dropped** and a `GOVERNANCE_ERROR` event is routed to stdout (regardless of which sink you configured).
- Severity escalates per session:
  - 1 failure → `warning`
  - 3 consecutive failures → `degraded`
  - Sink unreachable for the rest of the session → final `critical` `GOVERNANCE_ERROR` on session close
- The agent is **never blocked** by sink failures. Sink writes are observational; nothing in the agent execution path waits for sink success.

### Why no async sink?

The runtime is intentionally synchronous. Async sinks introduce ordering ambiguity (when does an event "really" land?) and complicate the failure semantics above. If you want decoupled async writes, point a sink at a local fan-out service and let that service handle async distribution.

## 14. What the Governor does NOT do

This list exists so you don't form wrong expectations:

- **Declared intent is untrusted input.** Sentience can identify when captured
  actions diverge from what an agent declared. It cannot determine whether the
  declaration itself was truthful or complete, or infer the agent's underlying
  motives. Recording an unsafe action correctly does not make the action safe or
  the agent trustworthy.
- **Governs supported agent actions, not model behavior.** It evaluates
  observable agent actions in business and operational workflows. It does not
  detect bias, toxicity, hallucinations, harmful content, or other
  model-output and content-safety issues.
- **Does not block agent execution.** Ever. Under any condition. The Governor is observational by design.
- **Does not modify tool calls or results.** It observes, records, and passes through.
- **Does not send telemetry, licensing checks, or usage data anywhere.** Governance runs with the network off. Two network-capable paths exist and neither is on the governance path: an optional sink that posts to an operator-configured URL, and a one-time launch-list prompt that sends an email address only if the operator enters one.
- **Does not aggregate governance state across sessions, machines, or a hosted plane.** The in-process cache is per-session and goes away when the session ends. Traces, reports, the profile and first-run state do persist on disk under `~/.sentience/`.
- **Does not infer or guess classifications.** If you don't supply a `classification_hook`, classifications are empty. "Unclassified" means unclassified — no fabricated metadata.
- **Does not require a Sentience account, API key, or network connection.** Python is the entire dependency footprint.
- **Does not enforce policies.** Rules are evaluated and surfaced; nothing is blocked. Real enforcement is a paid control-plane capability.
- **Does not write logs in any format other than the GovernanceEvent schema.** All events conform to a single Pydantic schema (`sentience_governor.schema.events.GovernanceEvent`).

### When the open-tier Governor is not useful

- You need enforcement (blocking, mutation prevention, policy control) — that's the paid control-plane.
- You need cross-session state or organizational memory — the open tier is per-session only.
- Your agent does not make tool calls (pure LLM prompt/response with no tools) — the Governor observes at the tool-call boundary; if there are no tool calls, there is nothing to observe.

## 15. Status, stability, and versioning

**This is a pre-release.** The current release fingerprint:

- Version: `0.3.0.2`
- License: Apache 2.0 (Crescere Labs, Inc.)

`0.3.0` is a backward-compatible additive release: governance Claude can
call. An opt-in MCP server (`sentience-mcp-server`, installed via `pip
install "sentience-governor[mcp]"`) exposes seven tools, including the
forward-looking `sentience_declare_intent(objective, scope)`. A declaration
suppresses POL-001 for subsequent matching activity at capture, while
pre-declaration events keep theirs (non-retroactive); it is recorded as
`intent_source = inferred` (agent-declared through MCP, content-untrusted,
not integrator-vouched) and fails closed on any uncertain session binding.
Register per project with `sentience init claude-code --mcp` (opt-in,
stdio, local); see the flip with `sentience demo declare-intent`. No
schema-version bump; no new event types (`declare_intent` reuses
`INTENT_DECLARED`); declaration-free capture is byte-identical to v0.2.9.
See §12.11 and `CHANGELOG.md` for release notes.

`0.2.9` is a backward-compatible additive release: tool-call visibility
and methodology. `sentience pulse` surfaces tool calls as a first-class
field (total, the four operation classes, and the top tools by call
count) and measured tool-token attribution (the tokens on turns that
fired a tool call, plus a per-tool full-turn-credit view). Attribution
stops at the turn — the model meters tokens per turn, not per tool — so
figures read "tokens on turns involving tool X," never per-tool spend. A
new `sentience explain` command states how Sentience counts, and the
`sentience open` summary splits policy violations from advisory flags and
self-labels its tool counts. No schema-version bump; no new event types;
no automatic network calls. Existing v0.2.4–v0.2.8.3, MCP, and LangChain
traces and tooling continue to work unchanged. See §12.9, §12.10, and
`CHANGELOG.md` for release notes.

`0.2.8` is a backward-compatible additive release — the first published
build of the Claude Code skills that expose six operator-invoked
`/sentience-*` commands inside the Claude Code chat: help, pulse,
status, profile, violations, and intent. The skills are installed by
`sentience init claude-code`, wrap the existing read-only CLI, and
instruct Claude to render the output verbatim (anything Claude adds is
interpretation, not Sentience measurement). The slash surface is
operator-invoked only, latest-session only, and read-only. Also in this
release: `sentience --version`; the first-install PATH-check fix;
status/list counts split into policy violations vs advisory flags with
a `status --json` reconciliation view; measured rule counts on sessions
that haven't ended yet; and empty-state messages that name the real
cause (token data is written at session end). No schema-version bump;
no new event types; no automatic network calls. Existing
v0.2.4–v0.2.6.1, MCP, and LangChain traces and tooling continue to work
unchanged. See §7 above and `CHANGELOG.md` for release notes.

`0.2.6.1` is a backward-compatible additive fast-follow. It adds
**Claude Code per-turn token-burn capture**: the hook now parses the
session transcript at `SessionEnd` and appends token-bearing
`CONTEXT_SNAPSHOT` events, so `sentience pulse` on a live Claude Code
session carries real per-turn token-burn attribution instead of
`no_signal`. No schema-version bump; no new event types; `schema_version`
stays at 1. Existing v0.2.4–v0.2.6, MCP, and LangChain traces and tooling
continue to work unchanged. See §7 and §12 above and `CHANGELOG.md` for
release notes.

`0.2.6` is a backward-compatible additive release. It introduces
**Sentience Pulse** (`sentience pulse`) — the v0.2.6 adoption
surface that composes the v0.2.4 undeclared-intent analyzer with a
new v0.2.6 policy-violation burn-rate analyzer plus an advisory-
flag-occurrence summary into a single shareable session report.
The burn-rate analyzer is also exposed standalone as `sentience
analyze policy-violations`. No schema-version bump; no new event
types; no new advisory-flag values; `schema_version` stays at 1.
Existing v0.2.4 and v0.2.5 wrappers, traces, and downstream tooling
continue to work unchanged. See §12 above for the full Pulse guide
and `CHANGELOG.md` for release notes.

`0.2.5` is a backward-compatible additive release. It introduces **operator-defined governance posture** via a YAML profile at `~/.sentience/profile.yaml` that the runtime (Claude Code hook, MCP wrapper, LangChain handler) evaluates against. Two new advisory-flag values fire on profile match (`TASK_BOUNDARY_CROSSED`, `HIGH_CONSEQUENCE_DETECTED`); three new optional fields land on the event envelope and `AGENT_REGISTERED` payload — all None-omitted, so sessions without a profile produce traces byte-identical to v0.2.4. The `compute_undeclared_intent_spend` analyzer becomes profile-aware and gains three new optional report sections (Profile, High-consequence operations, Task boundaries crossed). No schema-version bump; `schema_version` stays at 1. See §11 above for the full guide and `CHANGELOG.md` for release notes.

`0.2.4` shipped the first derived-metric analyzer (`sentience analyze undeclared-intent`) over the v0.2.3 token-attribution substrate. Existing v0.2.3 and v0.2.4 wrappers, traces, and downstream tooling continue to work unchanged under v0.2.5 and v0.2.6.

### What's stable

- The `GovernanceEvent` schema and its six event types
- The five `POL-001..POL-005` rule IDs and the ten advisory flag names (eight from v0.2.4 + `TASK_BOUNDARY_CROSSED` and `HIGH_CONSEQUENCE_DETECTED` added in v0.2.5)
- The public API surface of `wrap_mcp_client`, `ClassificationHint`, and `SentienceCallbackHandler`
- The `sentience-cli` command-line interface (positional file argument, `--no-colour` flag)
- The three sink implementations and their failure semantics
- The `intent_source` enum on `INTENT_DECLARED` events: `explicit` (integrator-declared via `stated_objective`), `inferred` (extracted from runtime invocation inputs by the LangChain callback handler), and `none` (no signal available). These three values, and the rule that input-extracted strings are never labelled `explicit`, are part of the schema contract.

### What might change before v1.0

- Additional fields on `ClassificationHint` (`write_type`, `detection_mechanism`) and a `persistence_targets` parameter on `wrap_mcp_client` — currently tracked as a separate task. **Backwards-compatible additive changes.**
- A configuration surface (env vars / config file) — currently the runtime takes parameters at construction time only. **Backwards-compatible additive changes.**
- A concrete MCP SDK binding (currently the abstraction-only `SentienceMCPAdapter` is shipped). **Additive — won't break existing adapters.**
- Possible new event type `TOOL_CALL_FAILED` for explicit failure observability. **Additive.**

### What will NOT change

- The fail-open contract (the wrapper never blocks the agent)
- The one-way isolation between `sentience_governor` and `sentience_sync`
- The "no automatic network calls from the runtime" guarantee

### Known limitations (read before deploying)

These are deliberate scope boundaries, not bugs. Each is tracked
for a future release; each is called out here so you can decide
whether the current behaviour fits your deployment shape.

**Session state is in-process only for `wrap_mcp_client` and
`SentienceCallbackHandler`.** The `SessionManager` and
`EventBuilder` hold chain state (`event_sequence_number`,
`previous_event_id`) in Python object memory for the lifetime of
the agent process. If that process crashes or is restarted mid-
session and resumes with the same `session_id`, the new
`SessionManager` starts fresh: sequence numbers restart at 0,
and the `previous_event_id` chain breaks at the restart boundary.

The Claude Code hook adapter (§7) does NOT have this limitation — it
reconstructs chain state from disk on every invocation via the
`sentience_governor.session_manager.resumption` primitive. The MCP
wrapper and LangChain adapter will adopt the same mechanism in a
future release (see the planned-fix note below).

- **What still works:** the sink file retains every event
  written before the crash. Nothing is lost on disk.
- **What breaks:** new events written after restart do not chain
  to the pre-restart ones. A trace viewer or auditor comparing
  the chain will see a discontinuity at the restart point.
- **Who this affects:** long-running agents under process
  supervisors (systemd, Kubernetes, supervisord), web servers
  where a single `session_id` spans multiple HTTP requests
  across worker restarts, any deployment where process lifetime
  is shorter than logical session lifetime.
- **Who this does NOT affect:** one-shot scripts, notebooks,
  batch jobs, demos — any usage where one Python process owns
  one full session from start to finish. This is the majority
  of today's usage, which is why this remains a deliberate open-tier
  scope boundary.
- **Workaround today:** treat the restart as a new session.
  Generate a fresh `session_id`; let the new session emit its
  own `AGENT_REGISTERED` + `INTENT_DECLARED` pair. The trace
  then contains two valid sessions that share context logically
  but not chain-wise.
- **Planned fix:** disk-based session resumption using the
  sidecar-index pattern already implemented in the Claude Code
  hook adapter. Opt-in parameter (`resume_session=True`) on
  `wrap_mcp_client`. Tracked in the Parking Lot; will land
  before any long-running deployment pattern is promoted to
  supported status.

## 16. Troubleshooting

### `/sentience-pulse` does not appear in Claude Code

**Cause:** The skills directory may have been created after Claude Code started, or skills were not installed.

**Fix:** Run `sentience init claude-code`, restart Claude Code, and type `/sentience-help`. If you used `--no-skills`, rerun without it. If you installed project-local skills with `--project`, make sure Claude Code trusts that workspace.

### `/sentience-pulse` says `sentience: command not found`

**Cause:** The slash command shells out to the local `sentience` binary, but Claude Code cannot find it on `$PATH`.

**Fix:** Confirm `sentience --version` works in a fresh terminal. If you installed with pipx, run `pipx ensurepath`, restart your shell, then restart Claude Code. Rerun `sentience init claude-code`; it will warn if `sentience` is not resolvable.

### Every CONTEXT_SNAPSHOT shows `POL-003` (`CONTEXT_UNCLASSIFIED`)

**Cause:** No `classification_hook` is wired up. The wrapper has no way to populate classification fields from the tool response, so it emits empty defaults, which (correctly) fires the rule.

**Fix:** Supply a `classification_hook` parameter to `wrap_mcp_client`. See §8.

### My tool call is a write but no `MEMORY_WRITE_ATTEMPT` event fires

**Cause:** Your tool name doesn't contain any of the persistence keywords (`database`, `vector_store`, `filesystem`, `logging`).

**Fix:** Either (a) rename the tool to include one of those keywords, or (b) wait for the `persistence_targets` parameter to land (tracked as future work).

### The `MEMORY_WRITE_ATTEMPT` event always shows `MEMORY_WRITE_CANDIDATE` advisory flag

**Cause:** This is **expected behaviour**, not a bug. The wrapper always emits `write_type=write_to_persistence_target` (it has no way to declare a write as `explicit_persist`), and the EventBuilder fires the `MEMORY_WRITE_CANDIDATE` advisory flag whenever `write_type=write_to_persistence_target`. It's an advisory nudge, not a policy violation.

**Fix:** Wait for the `write_type` field to be added to `ClassificationHint` (tracked as future work). Until then, this advisory flag is the wrapper's signature on every memory write event and can be safely treated as expected output.

### My tool raises an exception and there's no `CONTEXT_SNAPSHOT` event in the trace

**Cause:** This is **correct behaviour**. When the tool raises, the wrapper has only emitted `SCOPE_ASSERTED`. It does not emit `CONTEXT_SNAPSHOT` or `MEMORY_WRITE_ATTEMPT` because the tool call did not actually return any context to snapshot. The exception propagates to your code unchanged.

If you want explicit visibility into failed tool calls in the trace, that's tracked as a future event type (`TOOL_CALL_FAILED`).

### `sentience-cli` says "skipping malformed JSON on line X"

**Cause:** The trace file has a corrupted line. Could be from a partial write, a manual edit, or a sink failure mid-write.

**Fix:** Inspect line X of the file to see what's wrong. The viewer is robust — it skips bad lines and continues with the rest of the file.

### The wrapper raises `RuntimeError: Session not started. Use as context manager.`

**Cause:** You called `wrapped.send_tool_call(...)` outside an `async with wrapped:` block, or you forgot to enter the context manager.

**Fix:** Wrap your tool calls in `async with wrapped:`. The wrapper needs to construct an `EventBuilder` and emit `AGENT_REGISTERED` + `INTENT_DECLARED` before any tool calls can be processed, and the context manager is what triggers that setup.

### There's no `SESSION_END` event in my trace

**Cause:** Correct — there is no `SESSION_END` event *type* in the schema. The wrapper emits events at the **start** of a session (`AGENT_REGISTERED`, `INTENT_DECLARED`) and **per tool call** (`SCOPE_ASSERTED`, `CONTEXT_SNAPSHOT`, optionally `MEMORY_WRITE_ATTEMPT`).

**Note (v0.2.6.1+):** Claude Code's `SessionEnd` *hook* is now used — not to emit a session-end event, but to parse the session transcript and append token-bearing `CONTEXT_SNAPSHOT` events (per-turn token burn, keyed by `llm_turn_id`). So end-of-session processing does happen; it lands as additional `CONTEXT_SNAPSHOT`s, not a distinct event type. If you need a dedicated "session ended" event, file feedback — it's a reasonable schema addition for v1.

### `sentience pulse` reports `no_token_data` / no token burn on a Claude Code session

**Cause:** Per-turn token burn is captured by the Claude Code `SessionEnd` hook (v0.2.6.1+). If that hook isn't wired, the trace has tool-call events but no token-bearing `CONTEXT_SNAPSHOT`s — so `pulse` and the burn-rate analyzer report `no_token_data` / `no_signal`.

**Fix:** Run `sentience init claude-code` to wire all three hooks (`PreToolUse`, `PostToolUse`, `SessionEnd`), then start a **new** Claude Code session — capture only applies to sessions started after the hook is wired. (Subagent Task/Agent burn is excluded by design and disclosed as such in the report.)

### My LangChain trace shows `intent_source=inferred` even though I passed in `{"input": "..."}`

**Cause:** This is **expected behaviour**. The `SentienceCallbackHandler` extracts the `input` value at runtime from the chain's invocation dict. That string came from whoever invoked the chain (often the end user, or whichever layer constructed the LangChain call); it was not declared by the integrator ahead of time. The Governor reflects that distinction honestly — `intent_source=inferred` means "we got a string from runtime context, but we can't vouch for its meaning or authorization."

**Fix:** None needed if you understand what `inferred` means. If you want an integrator-declared objective on the LangChain path, that currently requires supplying it at the MCP level (via `stated_objective` on `wrap_mcp_client`) — the `SentienceCallbackHandler` does not accept a `stated_objective` parameter.

### I want to supply a `stated_objective` to `SentienceCallbackHandler` like I can on `wrap_mcp_client`

**Cause:** The LangChain handler does not expose a `stated_objective` constructor parameter. The only way a LangChain-wrapped agent gets an `INTENT_DECLARED` event with `stated_objective` set is through the runtime input-extraction path, which produces `intent_source=inferred`.

**Fix:** If you need `intent_source=explicit` for a LangChain agent, use the MCP wrapper path (`wrap_mcp_client(..., stated_objective="...")`) around the underlying tool client instead of or alongside the callback handler. Adding an integrator-declared objective parameter to `SentienceCallbackHandler` is tracked as future work.

---

For more depth: [`examples/README.md`](../../examples/README.md) covers the runnable demo against real Claude.
