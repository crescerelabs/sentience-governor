# examples/

> **Looking for the project overview?** → **[root `README.md`](../README.md)** for install, quickstarts, and the CLI commands table. This file only covers the runnable demo scripts in this directory.

---

Standalone runnable scripts that demonstrate the Sentience Governor in
realistic conditions. Nothing in this directory is part of the test
suite or the shipped runtime — these are demonstration artifacts only.

## Scripts

### `claude_demo.py`

Runs a real Claude tool-use loop through `wrap_mcp_client` and writes
a governance trace to a local file. Pipe the trace into `sentience-cli`
to see what Claude actually did under governance.

**Quick start:**

```bash
# Install the demo extras (one time)
pip install -e ".[demo]"

# Set your Anthropic API key
export ANTHROPIC_API_KEY=sk-ant-...

# Run the demo
python examples/claude_demo.py

# View the trace
sentience-cli /tmp/sentience-demo.jsonl
```

**What it does:** defines three fake tools (`crm.get_customer`,
`crm.fetch_usage`, `vector_store.upsert`), wires them through the
Sentience MCP wrapper with a `classification_hook` that injects
classification metadata, then drives a short Claude conversation that
exercises all three tools. Total run time is about 10 seconds and a
few cents of Anthropic API credits.

**What it isn't:**
- Not a test (non-deterministic, depends on the live Claude API)
- Not a benchmark (the fake tools make any timing meaningless)
- Not a Sentience Cloud client (the trace is written locally; no sync)
- Not part of the package install (lives in `examples/`, not
  `sentience_governor/`)

**Environment overrides:**

| Variable | Default | Purpose |
| :-- | :-- | :-- |
| `ANTHROPIC_API_KEY` | (required) | Claude API credentials |
| `SENTIENCE_DEMO_SINK_PATH` | `/tmp/sentience-demo.jsonl` | Where to write the governance trace |
| `SENTIENCE_DEMO_MODEL` | `claude-sonnet-4-5` | Claude model alias to use |

**Exit codes:**

| Code | Meaning |
| :-- | :-- |
| 0 | Success |
| 1 | Missing `ANTHROPIC_API_KEY` or missing `anthropic` SDK |
| 2 | Anthropic API error (rate limit, auth, service unavailable) |
| 130 | KeyboardInterrupt |

**Known wrapper-layer artifact:** the rendered trace will show a `⚠`
marker on the `MEMORY_WRITE_ATTEMPT` event with the
`MEMORY_WRITE_CANDIDATE` advisory flag. This is **expected behaviour,
not a bug**. The wrapper hardcodes `write_type=write_to_persistence_target`
and the `EventBuilder` fires that advisory flag whenever that write
type is used. It is not a policy violation. The full explanation
lives in the test class docstring of
`tests/test_mcp_wrapper.py::TestWrapperAcceptance`.

### `claude_airtable_demo.py`

Like `claude_demo.py`, but the three tools talk to a real Airtable
sandbox base instead of in-process fakes. Every governance event in
the trace corresponds to a real HTTP round-trip to `api.airtable.com`;
every `MEMORY_WRITE_ATTEMPT` event corresponds to a row that actually
appears in Airtable's activity log. This is the demo to run when you
want **externally-verifiable receipts** that the trace is honest.

> **Operator guardrail.** This demo writes to the `Snapshots` table
> on every run. Use a sandbox base only. Do not point this at a
> production Airtable base.

**Prerequisites (one-time):**

1. Create an Airtable base named **Governor CRM Sandbox** (free plan
   is fine) with three tables — `Customers`, `Usage`, `Snapshots` —
   and a few seed rows in each.
2. Generate an Airtable Personal Access Token scoped to that base
   only, with `data.records:read` + `data.records:write` permissions.
   See https://airtable.com/create/tokens.

**Quick start:**

```bash
# Install demo extras (one time; same extras as claude_demo.py)
pip install -e ".[demo]"

# Set credentials
export ANTHROPIC_API_KEY=sk-ant-...
export AIRTABLE_API_KEY=pat...
export AIRTABLE_BASE_ID=appXXXXXXXXXXXXX

# Run the demo
python examples/claude_airtable_demo.py

# View the trace
sentience-cli /tmp/sentience-airtable-demo.jsonl
```

**What it does:** wires a real Airtable client (via `pyairtable`)
through the Sentience MCP wrapper with three tools (`crm.get_customer`,
`crm.fetch_usage`, `crm.write_snapshot_to_database`), then drives a
short Claude conversation that exercises all three. The write tool's
name contains `database`, which triggers the wrapper's existing
persistence-target heuristic and emits a `MEMORY_WRITE_ATTEMPT`
governance event alongside the Airtable row creation.

**What it isn't:**
- Not a test (non-deterministic, depends on the live Claude + Airtable APIs)
- Not a benchmark (network latency dominates)
- Not a Sentience Cloud client (the trace is written locally; no sync)
- Not part of the package install (lives in `examples/`, not
  `sentience_governor/`)

**Environment overrides:**

| Variable | Default | Purpose |
| :-- | :-- | :-- |
| `ANTHROPIC_API_KEY` | (required) | Claude API credentials |
| `AIRTABLE_API_KEY` | (required) | PAT scoped to the sandbox base |
| `AIRTABLE_BASE_ID` | (required) | Sandbox base ID (starts with `app`) |
| `SENTIENCE_DEMO_SINK_PATH` | `/tmp/sentience-airtable-demo.jsonl` | Where to write the governance trace |
| `SENTIENCE_DEMO_MODEL` | `claude-sonnet-4-5` | Claude model alias to use |

**Exit codes:**

| Code | Meaning |
| :-- | :-- |
| 0 | Success |
| 1 | Missing required env var, or missing `anthropic`/`pyairtable` SDK |
| 2 | Anthropic API error (rate limit, auth, service unavailable) |
| 3 | Airtable API error (auth, rate limit, base/table not found) |
| 130 | KeyboardInterrupt |

**Expected runtime:** ~8–15 seconds per run, depending on network
latency to Anthropic and Airtable. Longer is not a failure; it's the
real cost of real HTTP round-trips.

**Verification protocol.** A successful run produces three artifacts
that should agree with each other:

1. The governance trace (`/tmp/sentience-airtable-demo.jsonl`) —
   timestamped events linked by `previous_event_id`.
2. Airtable activity — the sandbox's `Snapshots` table has a new row
   whose `Created At` timestamp is within a few seconds of the
   trace's `MEMORY_WRITE_ATTEMPT` timestamp.
3. Anthropic console — [console.anthropic.com](https://console.anthropic.com/)
   shows a new API call in Usage for the wall-clock window of the run.

The trace is credible when all three agree; verification fails when
the timestamps diverge beyond ±10s, when the `Snapshot Key` in the
trace does not match the Airtable row, or when the expected event
chain is missing the `MEMORY_WRITE_ATTEMPT` event.

**Known wrapper-layer artifact:** same as `claude_demo.py` — the
`MEMORY_WRITE_ATTEMPT` event carries the `MEMORY_WRITE_CANDIDATE`
advisory flag. Expected behaviour under the current heuristic
detection model.

### `claude_langchain_demo.py`

Same idea as the other two demos, but attached through the **LangChain
callback handler** (Fold 1b) instead of the MCP wrapper (Fold 1a).
A real Claude LLM drives a real LangChain tool-use loop; the
`SentienceCallbackHandler` observes every event and emits a
governance trace.

**What this demo verifies:**

- The LangChain callback handler produces a valid governance trace
  end-to-end with a real LLM
- `intent_source=inferred` + `intent_confidence=inferred_low` appear
  on the INTENT_DECLARED event (the honest classification for the
  LangChain path — intent is extracted from the chain's invocation
  input, not integrator-declared)
- `CONTEXT_UNCLASSIFIED` + `POL-003` fire on every CONTEXT_SNAPSHOT
  (the LangChain path does not currently support a classification
  hook — all context arrives unclassified, honest and expected)

**Quick start:**

```bash
# Install demo extras (same extras as the other demos)
pip install -e ".[demo]"

# Set your Anthropic API key (no Airtable credentials needed)
export ANTHROPIC_API_KEY=sk-ant-...

# Run the demo
python examples/claude_langchain_demo.py

# View the trace
sentience-cli /tmp/sentience-langchain-demo.jsonl
```

**What it does:** defines two fake in-process tools
(`crm_get_customer`, `crm_fetch_usage`) as LangChain `@tool`
functions, binds them to `ChatAnthropic`, attaches the
`SentienceCallbackHandler` as a callback, and drives a short
Claude tool-use loop. Total run time is about 10 seconds and a few
cents of Anthropic API credits. No external service calls beyond
Anthropic.

**What it isn't:**

- Not a test (non-deterministic, depends on the live Claude API)
- Not an Airtable demo (use `claude_airtable_demo.py` for that —
  this demo is specifically about verifying the callback handler)
- Not a test of persistence detection or MEMORY_WRITE_ATTEMPT
  (fake tools don't match persistence-target keywords; that's
  covered in the Airtable demo)
- Not part of the package install (lives in `examples/`, excluded
  from the wheel)

**Environment overrides:**

| Variable | Default | Purpose |
| :-- | :-- | :-- |
| `ANTHROPIC_API_KEY` | (required) | Claude API credentials |
| `SENTIENCE_DEMO_SINK_PATH` | `/tmp/sentience-langchain-demo.jsonl` | Where to write the governance trace |
| `SENTIENCE_DEMO_MODEL` | `claude-sonnet-4-5` | Claude model alias to use |

**Exit codes:**

| Code | Meaning |
| :-- | :-- |
| 0 | Success |
| 1 | Missing `ANTHROPIC_API_KEY`, `langchain-anthropic`, or `langchain-core` |
| 2 | LangChain / Anthropic runtime error |
| 130 | KeyboardInterrupt |

**Expected trace characteristics** (different from the other two
demos, on purpose):

- `INTENT_DECLARED` event shows `source=inferred`,
  `confidence=inferred_low` — not `explicit`
- Every `CONTEXT_SNAPSHOT` event fires `⚠` with `POL-003`
  (`CONTEXT_UNCLASSIFIED`)
- No `MEMORY_WRITE_ATTEMPT` event (no write tools)
- The session summary shows policy violations (the POL-003s) —
  this is the honest LangChain first-run experience, matching the
  failure-first walkthrough in `docs/guide/sentience_governor.md` §3.1

