# Sentience Governor for Pydantic AI

**Agent Execution Evidence at the Pydantic AI runtime execution boundary.**

Governance-relevant evidence of what an agent actually dispatched at runtime,
against the declaration it recorded before the run. This is a different
artifact from logging, tracing or observability, and it is meant to sit
alongside them rather than replace them.

[![PyPI](https://img.shields.io/pypi/v/pydantic-ai-governor.svg)](https://pypi.org/project/pydantic-ai-governor/)
[![Python](https://img.shields.io/pypi/pyversions/pydantic-ai-governor.svg)](https://pypi.org/project/pydantic-ai-governor/)
[![Pydantic AI](https://img.shields.io/badge/pydantic--ai-2.37.x-e520e5.svg)](https://pydantic.dev/docs/ai/overview/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/crescerelabs/sentience-governor/blob/main/LICENSE)
[![integration tests](https://github.com/crescerelabs/sentience-governor/actions/workflows/integration-pydantic-ai.yml/badge.svg)](https://github.com/crescerelabs/sentience-governor/actions/workflows/integration-pydantic-ai.yml)
[![Governor's Log](https://img.shields.io/badge/Governor's%20Log-read-1f6feb.svg)](https://getsentience.ai/governor-log)

Sentience Governor itself needs no Sentience account and no Sentience API
key. Whatever model or provider you give Pydantic AI has its own credential
requirements, and this changes none of them. By default, your traces stay on
your machine.

---

## What's new

**0.1.0** is the first release of this distribution. It adds a Pydantic AI
capability that records what an agent dispatched at runtime against the
declaration state recorded before the run, which may be an objective and
scope or the absence of a valid declaration: a session per run, an assertion
before each tool is dispatched, a snapshot after each normal return, and
per-turn token usage read from the model response. Declaring a run's
objective is one metadata block; classifying a tool is one more. Nothing is
inferred from a tool's name. Non-interference was verified across the tested
execution paths for this release; see
[Observation and non-interference](#observation-and-non-interference).

This README lists releases of `pydantic-ai-governor` only.
`sentience-governor` is versioned and released separately, and its releases
do not appear here.

---

## What Agent Execution Evidence means

The question a governance reviewer asks is *did this agent do what it said it
was going to do?* Answering it needs two things recorded **together**. The
**declaration**: what this run said it was for, or that it declared nothing,
recorded before the run starts. The **dispatch record**: each tool call the
framework validated and was about to execute, recorded at the boundary where
execution actually happens.

A tracing system can carry either of those, and a modern one can carry
arbitrary metadata, so this is not a claim about what tracing is capable of.
The difference is what this integration records by default and where. It
records the declaration first, before anyone knows what the agent will do,
then records each dispatch at the execution boundary, and keeps the two
joinable. A tool call outside the declared scope is visible as such because
of that pairing, not because a field was named well.

Sentience Governor records. It does not intervene. See
[What the evidence does and does not prove](#what-the-evidence-does-and-does-not-prove).

## Installation

```bash
pip install pydantic-ai-governor
```

This installs `sentience-governor` and `pydantic-ai-slim` as dependencies.
See [Compatibility](#compatibility) for the supported version ranges.

**It does not install model or provider SDKs, and it is not meant to.** Your
Pydantic AI application already declares whichever provider it uses, through
Pydantic AI's own extras or a full `pydantic-ai` install. Adding this package
to an application that already runs changes nothing about that.

In a fresh environment, install the provider your example needs alongside it.
For the OpenAI model used below:

```bash
pip install pydantic-ai-governor "pydantic-ai-slim[openai]>=2.37.0,<2.38"
```

## Attaching the capability

```python
from pydantic_ai import Agent
from pydantic_ai_governor import SentienceGovernor

agent = Agent(
    "openai:gpt-4o",
    capabilities=[SentienceGovernor()],
)

result = await agent.run("Reconcile the August invoices")
```

That is the whole integration. Every run of this agent now opens its own
Sentience Governor session and writes a trace.

### Constructor defaults

The constructor takes three optional keyword arguments, and every one has a
usable default:

```python
SentienceGovernor(
    objective=None,             # what runs of this agent are for
    scope=None,                 # the systems runs of this agent may touch
    agent_id="pydantic-ai-agent",
)
```

`objective` and `scope` here are **defaults for every run**, suitable when an
agent has one standing purpose. `agent_id` names the agent in the evidence;
change it when one process runs several distinct agents and you want them
distinguishable.

**Every session records its declaration state before execution, including
the absence of a valid declaration.** A run with no objective from either
source is recorded as having declared none. That is a supported state and a
real reading, not a gap in the record, and nothing invents an objective to
fill it.

### Per-run objective and scope

Most agents do different things on different runs. Declare per run, and the
run's declaration overrides the constructor defaults:

```python
result = await agent.run(
    "Reconcile the August invoices",
    metadata={"sentience_governor": {
        "objective": "Reconcile August invoices",
        "scope": ["crm", "billing"],
    }},
)
```

Both keys are optional and fall back key by key: a block that supplies only
`objective` keeps the constructor's `scope`.

**A malformed block is never silently ignored.** If the block is not a
mapping, if a key is misspelled, or if a value has the wrong type, the run
continues normally and you get a `UserWarning` at the keyboard naming the
field and the contract it broke. The warning never reproduces the value you
wrote, since a rejected declaration is exactly where sensitive text might
have been.

**What governs the run after a rejection depends on your constructor.** The
malformed block is rejected atomically, so a good `objective` beside a broken
`scope` yields no half-declaration. Then:

| Constructor supplied | After a malformed per-run block |
| :-- | :-- |
| A valid `objective` (and `scope`) | **Those defaults remain in force**, and the run is declared under them |
| Nothing | The run is recorded as **undeclared** |

So a rejected block does not by itself make a run undeclared. It falls back,
and the warning says which of the two happened.

## Classifying tools

A tool call is recorded whether or not you classify it. Classification is how
you tell the evidence what the call *is*:

```python
from pydantic_ai.tools import Tool

crm_tool = Tool(
    crm_fetch,
    metadata={"sentience_governor": {
        "operation": "READ",              # READ, WRITE, DELETE or EXECUTE
        "target_system": "crm",           # matched against declared scope
        "classification": ["internal"],   # data classifications you assert
    }},
)
```

All three keys are optional. `operation` must be one of the four values
exactly, in upper case: `"read"` is not `"READ"`, and accepting the near miss
would mean recording something you did not write.

### When classification is missing

This is the case worth understanding, because the honest answer is not the
convenient one.

**Nothing is inferred from the tool's name.** A tool called
`db_delete_record` does not become a delete against a database. Name-based
inference would put a guess into a record whose whole value is that it
contains no guesses.

So an unclassified call is recorded as **unclassified**. The `target_system`
falls back to the tool's own name, which is a fact about the call rather than
a bucket someone guessed, and the snapshot is flagged as unclassified, which
is what surfaces in review.

#### What the operation field says, if you read the raw trace

When no operation is declared, the integration treats it internally as
`UNKNOWN`. Current Sentience Governor core cannot serialize `UNKNOWN`:
`operation_type` is required and has four members. At the core boundary the
undeclared case is therefore written as:

| | `operation_type` | `asserted_permissions` |
| :-- | :-- | :-- |
| Explicitly declared `READ` | `READ` | `["read"]` |
| **No operation declared** | `READ` | `[]` |

**That `READ` is a compatibility representation, not an observed read.** It
does not mean the tool read anything. `READ` is used because it is the only
non-mutating member, so an undeclared call is not written as a mutation the
developer never claimed.

The empty `asserted_permissions` is what distinguishes the two rows **for a
human reading the trace**. It is not a mechanism: no policy rule or analyzer
in this release interprets empty permissions as `UNKNOWN`, and nothing here
should be read as saying they do. The mapping exists only while core has no
undeclared-operation semantic. Once Governor core provides a first-class
one, **a future release of this integration can remove the mapping**; a
published release does not change how it serializes on its own.

A **malformed** classification is not the same as a missing one, and the two
are kept apart. A developer who wrote metadata believes they classified
something, so **an invalid value in a recognized field rejects the block
atomically**: a good `operation` beside a broken `classification` yields
nothing, you get a `UserWarning` and a governance error naming the field,
and the call falls back to unclassified.

One narrower case: an unrecognized key alongside otherwise valid fields is
reported, and every field that independently validates is kept. Discarding a
truthful classification because of an unrelated stray key would make the
evidence worse while protecting nothing.

## Sessions

**One run, one session.** A Pydantic AI run's own `run_id` is the Sentience
Governor session id, so the two systems agree on identity with no mapping
table to drift. When the run ends, on the success path or the error path
alike, the session ends.

**Concurrent runs stay separate.** Sibling runs of one agent, in flight
together on one event loop, each get their own session and their own trace
file. Neither can pick up the other's tokens, model, provider or turn
identity, and per-session sequence numbers stay unique and gapless within
each. The same holds for parallel tool calls inside a single model response:
each call carries its own identity, and no per-call state is shared between
them.

**A resumed run is a new session, deliberately.** When a deferred tool call
is approved and you resume, Pydantic AI starts a new run with a new `run_id`,
so Sentience Governor opens a new session. The two are not merged. This
release introduces no cross-run correlation, and presenting two runs as one
session would be a claim about continuity that nothing here verifies.

## What evidence is produced

Each session writes one append-only JSONL file:

```
~/.sentience/traces/pydantic-ai/<run_id>.jsonl
```

Within it, per session:

| Record | When |
| :-- | :-- |
| Agent registration | Session open |
| Declared intent | Session open, recording the declaration state: the objective and scope, or that none was declared |
| Scope assertion | After validation, immediately before a tool is dispatched, keyed by `tool_use_id` |
| Context snapshot, per tool | After a tool returns normally, keyed by the same `tool_use_id` |
| Context snapshot, per model turn | After each model response, carrying that turn's measured token usage, model and provider identity, and the tool call ids that turn issued |

The ordering carries meaning. A scope assertion is written after the
framework has validated the call and immediately before dispatch, so it
records a call that really was about to run. The tool snapshot is written
only on a normal return.

**Read the pair by identity, not by position.** A model response can issue
several tool calls at once, so "the next snapshot" is not necessarily the one
belonging to a given assertion. The join is `tool_use_id`:

> A scope assertion with **no matching tool context snapshot for the same
> `tool_use_id`** means no normal return was observed for that call.

That is the whole claim. It does not say why the call failed to return
normally, and it does not distinguish a raised exception from anything else.
There is still no execution-outcome field in the schema, and none is being
inferred here.

### Estimated context, measured usage

Two numbers on these snapshots share a field name and are not the same kind
of evidence. Keeping them apart matters if you sum them.

| Snapshot | `context_size_tokens` | Source |
| :-- | :-- | :-- |
| Per tool | **Estimated** | The established Sentience Governor estimator, the same one the shipped MCP wrapper uses. A tool boundary has no measured model-input count |
| Per model turn | **Measured** | `ModelResponse.usage.input_tokens` |

Per-turn `llm_prompt_tokens` and `llm_completion_tokens` are also **measured**,
from `ModelResponse.usage`. On a model-turn snapshot, `context_size_tokens`
and `llm_prompt_tokens` carry the same measured number: one measurement under
two field names, not two independent readings.

**An estimate and a measurement are not interchangeable.** Do not add a tool
snapshot's estimated context to a model turn's measured usage and present the
total as measured token spend.

Governance errors, including the malformed-metadata cases above, are routed
by the core package to stdout rather than into the trace file. That is the
core package's routing decision, and this integration follows it rather than
working around it.

The trace uses the normal Sentience Governor format, and compatibility was
verified against the analyzers exercised in this release's test suite:
`compute_pulse`, the undeclared-intent analysis, and the token and tool
attribution paths those cover. Analyzers outside that set are untested here
rather than known to differ.

## What the evidence does and does not prove

**It records** that a tool call passed the framework's validation and was
dispatched, within a session whose declaration state was recorded before the
run began, with the measured token usage of each model turn.

**It does not prove the call succeeded.** A scope assertion with no matching
tool snapshot for the same `tool_use_id` means no normal return was observed
for that call. It does not say why.

**It does not establish object-level scope.** `target_system` is a declared
label, not a verified assertion about which records or rows a call touched.
A call declared against `crm` is recorded as such; nothing here checks that
it stayed inside any particular customer's data.

**It does not intervene.** This capability records and flags, and takes no
action on a tool call before or after it runs: nothing is halted, refused,
delayed or altered. It must not be relied on as a control. Flags are
advisory signals for review.

**It does not rank or grade what it recorded.** A flag says what was
observed. Deciding what that is worth is the reviewer's job, and a number
attached here would only be a guess wearing a decimal point.

**It says nothing about a run it did not observe.** Evidence is bounded to
sessions where the capability was attached.

## Observation and non-interference

**Across the tested execution paths for this release, attaching the
capability did not change agent output, message count, token usage,
exception propagation, retries, deferral, streaming or control flow.** Those
paths are: normal returns, text-only runs, parallel tool calls in one model
response, retries, raised tools, validation failures, streaming, deferred and
resumed execution, and concurrent runs. Each is compared against the same run
without the capability attached, and a raised tool propagates the same
exception type with the same message. The model response is returned exactly
as it arrived.

That is evidence from a tested surface, and it is stated that way on purpose.
It does not assert the same about every path a Pydantic AI agent can take,
only about the ones listed above.

**Non-interference is about the agent, not about silence.** Malformed
metadata deliberately produces a warning and a governance error. That is
additive evidence plus a message to the developer, and it changes nothing
about tool returns, agent output, retries, deferral, streaming or control
flow of any kind.

## Compatibility

| Requires | Range |
| :-- | :-- |
| `sentience-governor` | `>=0.3.1.2,<0.3.2` |
| `pydantic-ai-slim` | `>=2.37.0,<2.38` |
| Python | `>=3.10` |

These bounds are deliberate published compatibility contracts rather than
defaults, and both ceilings are narrow on purpose. A wider `sentience-governor`
ceiling would let a future core release change this distribution's observable
behavior without a new release of it. A wider `pydantic-ai-slim` ceiling would
assert compatibility with releases this integration has not yet verified,
including ones published after this release.

They are widened only after measured verification against a new version, and
only in a new release of this distribution. A bound is never relaxed in place.

Continuous integration runs the suite on Python 3.10, 3.11, 3.12 and 3.13,
with separate floor and ceiling dependency legs.

**Today both legs resolve to `pydantic-ai-slim==2.37.0`**, because no later
2.37.x release has been published inside the `<2.38` ceiling. The legs are
kept separate on purpose: when a compatible 2.37.x release appears, the
ceiling leg picks it up automatically and the range is exercised at both ends
without anyone editing the workflow.

## Relationship to `sentience-governor`

This is an independent distribution that depends on the core package. The
dependency runs one way only:

```
pydantic-ai-governor  ->  sentience-governor
                      ->  pydantic-ai-slim
```

Core acquires no Pydantic AI dependency, mandatory or optional. The two
distributions have independent versions, changelogs, artifacts, tags and
release trains, and both follow the same release discipline. Releasing one
does not require rebuilding, versioning, tagging or republishing the other.

`pydantic-ai-governor` is a distribution name following the ecosystem's
`pydantic-ai-<name>` convention. It does not imply that Pydantic owns,
operates or endorses Sentience Governor.

## Governor's Log

The reasoning behind how Sentience Governor records, and what we are learning
from pointing it at real agent sessions, is published as
[Governor's Log](https://getsentience.ai/governor-log): a technical
publication about AI agents, their architectures, and the systems needed to
understand and govern them.

## License

Apache-2.0. See
[LICENSE](https://github.com/crescerelabs/sentience-governor/blob/main/integrations/pydantic-ai-governor/LICENSE).
