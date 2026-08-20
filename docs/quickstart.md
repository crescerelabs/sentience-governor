# Quickstart

> Sentience observes execution when actions happen, not after the fact.

## Recommended: Claude Code hook (fastest way to see value)

**Time to first trace: about one minute.**

Claude Code invokes tools constantly — file reads, edits, bash commands, web fetches, MCP servers. One command wires Sentience in, and every tool call becomes a local execution event you can inspect later.

### Step 1 — Wire the hook

```bash
sentience init claude-code             # wire the current project
# or target a specific project:
sentience init claude-code /path/to/project
```

This writes (or idempotently merges into) `.claude/settings.json` with the correct hook-binary path for your install. It never clobbers existing hooks or settings; re-running refreshes the skills safely (a hand-edited skill is preserved unless you pass `--force`).

It also installs six Claude Code skills into `~/.claude/skills/`, exposed as `/sentience-*` slash commands (new in 0.2.8). After it finishes, **restart Claude Code** and type:

```text
/sentience-help     # what the commands do and their boundaries
/sentience-pulse    # one-command report for the latest captured session
```

The commands are operator-invoked only, scoped to the latest captured session, and read-only. Pass `--no-skills` to wire hooks without them, `--project` for a project-local install you can share via git, or `--force` to overwrite a hand-edited skill.

By default, hooks are wired into the initialized project's `.claude/settings.json`, while skills install to your personal `~/.claude/skills/`; `--project` makes the skills project-local too.

<details>
<summary>Prefer to wire it by hand?</summary>

Create or edit `.claude/settings.json` in your project (or `~/.claude/settings.json` user-global). Use the absolute path to `sentience-claude-code-hook` if it isn't on Claude Code's `$PATH`. **All three hooks are required** — omit `SessionEnd` and per-turn token burn is never captured, so `sentience pulse` reports `no_signal`:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "", "hooks": [{"type": "command", "command": "sentience-claude-code-hook"}]}
    ],
    "PostToolUse": [
      {"matcher": "", "hooks": [{"type": "command", "command": "sentience-claude-code-hook"}]}
    ],
    "SessionEnd": [
      {"matcher": "", "hooks": [{"type": "command", "command": "sentience-claude-code-hook"}]}
    ]
  }
}
```

(`sentience init claude-code` wires all three for you.)

</details>

Claude Code now invokes `sentience-claude-code-hook` before and after every tool call, and at session end to capture per-turn token burn.

### Step 2 — Run Claude Code as normal

Open Claude Code. Do whatever you would normally do — ask it to read a file, run a test, edit some code.

Exit when done.

### Step 3 — Review what happened

Inside Claude Code:

```text
/sentience-pulse    # one-command report for the latest captured session
```

Or from a terminal:

```bash
sentience status                    # did the hook actually fire?
sentience list                      # which sessions exist?
sentience open --latest --summary   # one-screen view of the latest session
sentience pulse --latest            # one-command report
```

You should see:
- Sessions listed
- Events captured
- A summary explaining what the agent did

If you see nothing → [Troubleshooting](./troubleshooting.md).

Per-session traces persist at `~/.sentience/traces/claude-code/`.

### What you will see

Every `Bash`, `Edit`, `Write`, `Read`, `Grep`, `Glob`, `WebFetch`, `WebSearch`, and `mcp__<server>__<tool>` call Claude Code made, evaluated against the default policy rules.

### Step 4 (optional) — Edit your governance profile

New in v0.2.5: a governance profile at `~/.sentience/profile.yaml` lets you declare what you expect of any agent on this machine. One file, applies to every governed session — Claude Code hook, MCP wrapper, LangChain handler. Authored once; travels with you.

```bash
sentience profile init       # create a starter profile (one-time)
sentience profile view       # see what's there
sentience profile edit       # tune it in $EDITOR
```

The profile shapes three things: when undeclared intent is surfaced, when the agent has crossed a task boundary (directory shift, file-type shift, read-to-write transition, time gap), and which tools should be treated as high-consequence (operator-authored regex patterns). All signals are observational; nothing is blocked.

If you skip this step, sessions still capture exactly as v0.2.4 — the profile is strictly additive. See the [user guide §11](https://github.com/crescerelabs/sentience-governor/blob/main/userdocs/sentience_governor.md#11-governance-profiles) for the schema reference, CLI verbs, and a runnable closed-loop example.

### Step 5 (optional) — Analyze a session

Once you have a session, you can run a derived-metric analyzer over it:

```bash
sentience analyze undeclared-intent --latest
```

This shows how much compute in the session was attributed to reasoning turns that touched execution outside the session's declared operational intent. On Claude Code sessions with the `SessionEnd` hook wired, token attribution is available and the analyzer can show per-turn breakdowns. If you see `no_token_data`, rerun `sentience init claude-code`, start a new Claude Code session, and try again. When a profile is loaded, the analyzer also surfaces three new sections: Profile (fingerprint), High-consequence operations, and Task boundaries crossed. See the [user guide §10](https://github.com/crescerelabs/sentience-governor/blob/main/userdocs/sentience_governor.md#10-analyzers--derived-metrics-over-captured-traces) for the full guide and the JSON output schema.

### Step 6 (recommended) — Run your first pulse

New in v0.2.6 and available inside Claude Code in v0.2.8: `sentience pulse` is the one-command session report. It composes the undeclared-intent analyzer above, a policy-violation burn-rate analyzer, and an advisory-flag summary into a single report you can read, save, or share.

Inside Claude Code:

```text
/sentience-pulse
```

From a terminal:

```bash
sentience pulse --latest          # one-command session report
sentience pulse --latest --save   # save the Markdown report to ~/.sentience/reports/
sentience pulse --showcase        # bundled clean-session example, works on any install
```

Since v0.2.9 the pulse also shows **tool calls** (total, the four operation classes, and the top tools) and **tool-token attribution**: the tokens on turns that fired a tool call, plus a per-tool full-turn-credit view. Attribution stops at the turn (the model meters tokens per turn, not per tool), so figures read "tokens on turns involving tool X," never per-tool spend. Run `sentience explain` to see exactly how these numbers are counted.

Each section ends with a one-line "Why it matters" translation, so the report reads cold — paste it into an issue, a Slack message, an advisor update, or a customer / investor proof point without context from the operator who ran the session. For a fresh-operator first session (default profile, well-behaved agent) you'll usually see status `ok` with no policy violations and an Interpretation block that names the recurring-value point explicitly: *your session was observable, your profile was loaded, and no policy rules fired.*

The Markdown report's footer includes a one-line email-list sign-up prompt (suppress globally with `SENTIENCE_NO_SYNC_PROMPT=1`). See the [user guide §12](https://github.com/crescerelabs/sentience-governor/blob/main/userdocs/sentience_governor.md#12-sentience-pulse) for the full walkthrough plus three pre-rendered showcase scenarios (clean / missing-intent / mixed-violations).

---

## Advanced: Agent wrapper

If you're just evaluating Sentience, skip this section.

**Time to first trace: about three minutes.**

If you're building with the MCP SDK or LangChain, wrap the agent once and every tool call becomes a governance event.

### MCP client

```python
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import SinkWriter, StdoutSink
from sentience_governor.wrapper.mcp import (
    SentienceMCPAdapter,
    wrap_mcp_client,
)

session_manager = SessionManager()
cache = InProcessCache()
sink = SinkWriter(StdoutSink())

adapted = SentienceMCPAdapter(
    delegate=your_sdk_client,
    call_fn=lambda client, name, args: client.call_tool(name, args),
)

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
    stated_objective="Generate Q1 customer report",
)

async with wrapped:
    result = wrapped.send_tool_call("crm.get_customer", {"id": "123"})
```

Three steps: import, assemble, wrap.

### LangChain

`SentienceCallbackHandler` duck-types LangChain's `BaseCallbackHandler`, so you attach it as a callback on any LangChain-based agent.

```python
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import SinkWriter, StdoutSink
from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler

session_manager = SessionManager()
cache = InProcessCache()
sink = SinkWriter(StdoutSink())

handler = SentienceCallbackHandler(
    agent_id="my-agent",
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

For `create_react_agent`, `SentienceMiddleware` wraps the same handler:

```python
from sentience_governor.wrapper.langchain_adapter import SentienceMiddleware

middleware = SentienceMiddleware(handler)
agent = create_react_agent(llm=llm, tools=tools, prompt=prompt, middleware=[middleware])
```

#### Token tracking (optional)

`SentienceCallbackHandler` automatically captures per-turn LLM token usage from LangChain responses via `on_llm_start` and `on_llm_end` and attaches it to subsequent tool-call events. Stable `llm_turn_id` values keep token aggregation accurate across multi-tool-call sessions (consumers should dedupe by `(session_id, llm_turn_id)` before summing token fields).

For `create_react_agent`, the same `SentienceMiddleware` instance also exposes `awrap_step` — register it to aggregate token usage across messages within a single LangGraph step. The middleware's existing `awrap_tool_call` continues to work unchanged for users who don't opt in.

Anthropic via LangChain carries token data on both `usage_metadata` (canonical fields) and `response_metadata['usage']` (cache fields). The handler merges both shapes so cache reads/writes are preserved.

### Sinks — where events go

- **`StdoutSink`** — prints every event to stdout
- **`FileSink`** — append NDJSON to a file
- **`HttpLocalSink`** — POST each event to a local HTTP endpoint

All three are fail-open.

### View the trace

```bash
my-agent | sentience-cli          # pipe from stdout-sink
sentience-cli agent-trace.jsonl   # read from FileSink output
```

---

## Optional: Cloud telemetry note

The experimental Sync cloud telemetry CLI was removed in v0.2.8.3. Sentience Governor is local-first. Nothing is uploaded automatically.


---

## Next

- [Commands reference →](./commands.md)
- [Troubleshooting →](./troubleshooting.md)

---

Stuck? → [Troubleshooting](./troubleshooting.md)
