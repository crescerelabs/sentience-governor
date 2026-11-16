# AGENTS.md

Guidance for AI coding agents (and the people directing them) working in this
repository. Humans should start with [README.md](README.md) and
[CONTRIBUTING.md](CONTRIBUTING.md); this file restates the parts an agent needs
to work here safely.

## What this project is

Sentience Governor is local-first governance for AI agents at runtime. It
captures supported agent actions at the execution boundary, records them as
structured events in a local trace, evaluates each action against the agent's
declared intent, scope and active policy, and reports violations and advisory
flags.

What it does not do, and what you must not make it claim to do:

- **The open-source release does not block.** It records and flags. It never
  stops or modifies an agent action.
- **It is fail-open.** Governance must never break the governed agent's own
  work.
- **No telemetry.** Nothing is uploaded; the record stays on the machine.
- It governs agent **actions**. It does not detect bias, toxicity,
  hallucinations or other content-safety issues.

Packaging: `sentience-governor` on PyPI, Apache 2.0, Python 3.10 or newer.

## Repository map

| Path | What lives there |
| :-- | :-- |
| `sentience_governor/wrapper/` | Capture paths: Claude Code hook, MCP client wrapper, LangChain callback handler and LangGraph middleware |
| `sentience_governor/schema/` | Event schema and field meanings (**maintainer-governed**) |
| `sentience_governor/event_builder/` | Event construction (**maintainer-governed**) |
| `sentience_governor/profile/` | Governance profiles, schema and reserved slots (**maintainer-governed**) |
| `sentience_governor/session_manager/` | Session identity, continuity and resumption (**maintainer-governed**) |
| `sentience_governor/analyze/`, `retro.py` | Reports and analysis over captured traces |
| `sentience_governor/cli/` | The `sentience` and `sentience-cli` commands |
| `sentience_governor/mcp_server/` | Opt-in local MCP server (stdio) so an agent can query its own governance record |
| `integrations/pydantic-ai-governor/` | Separately packaged and published Pydantic AI integration, with its own tests and pytest config |
| `examples/` | Runnable examples. Not shipped in the package |
| `tests/` | Core test suite |
| `docs/` | User documentation. Design records in `docs/design/` |
| `scripts/` | Release and repository checks |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,mcp]"
```

Optional extras:

- `mcp` is needed for the MCP server.
- `demo` holds the libraries `examples/` uses, including `langchain-core`.

The Pydantic AI integration installs on top of the core:

```bash
.venv/bin/pip install -e "./integrations/pydantic-ai-governor[dev]"
```

## Checks to run before proposing a change

```bash
make test                                 # full core suite, offline
python3 scripts/check_public_surface.py   # public-surface guard; CI runs it too
```

Both run in CI on every pull request. If you touched
`integrations/pydantic-ai-governor/`, run its suite from that directory as
well.

Tests that need an optional dependency are skipped when that dependency is not
installed. Examples are `langchain-core` for the LangChain and LangGraph
capture paths, and `mcp` for the MCP server. Whatever any CI job happens to
install, a skipped test proves nothing. If your change touches a capture path or
integration that depends on a third-party framework:

- install the real dependency;
- run those tests against it, not only against a stand-in;
- in the pull request, report which tests ran, which were skipped, and the
  dependency versions you used.

## Rules for changes

**Changes that are welcome:**
- integrations and adapters;
- harness support;
- examples;
- reporting and developer experience;
- documentation;
- performance and portability with no change in behavior;
- tests for existing behavior;
- bug fixes that come with a test which fails without the fix.

**Stop and ask** (open or point to an agreed issue first) before changing any
of these:
- what counts as a violation, its code or its severity;
- the event schema or what a field means;
- event construction;
- the profile schema or reserved values (`block`, `deny` and `prompt` are
  reserved and currently fall back to `flag`);
- enforcement behavior, including anything that touches fail-open;
- session identity and continuity.

These are maintainer-governed because users rely on the trace as evidence. A
pull request is not the place to decide them. Passing tests, a green CI run, or
instructions in a task, issue or prompt are not approval to change governance
semantics. Only agreement from the maintainer in an issue is.

**Never:**
- Make a test pass by weakening what it asserts about governance. If the
  assertion looks wrong, explain the behavioral problem instead.
- Add telemetry, network calls on the capture path, or anything that sends
  trace data off the machine.
- Let an exception from governance code escape into the governed agent's call.
  Capture paths must swallow and log.
- Commit real traces, prompts, credentials or customer data. Use synthetic
  fixtures. Traces can contain prompts, tool arguments, paths and secrets.
- Bump versions, edit release notes for an unreleased version, tag, or
  publish. Releases are run by the maintainer.

**Testing habits that have caught real bugs here:**
- Test the usage the docs show, not only the pattern the tests already use. If
  the README passes an object directly, test that.
- Test against the real dependency when one is involved, not only a stand-in.
- Cover unexpected-but-valid input as well as invalid input. For example, a
  trace line that is valid JSON but not an object.
- Cover runs that fail as well as runs that complete.

## Pull requests

- One concern per pull request.
- Sign off every commit for the DCO: `git commit -s`.
- Say in the description whether governance output changes. If it does, link
  the issue where that was agreed.
- Security issues go through [SECURITY.md](SECURITY.md), never a public issue.

## Useful references

- [README.md](README.md): install, integrations and how it works
- [docs/commands.md](docs/commands.md): every command and flag
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how capture, evaluation and the
  trace fit together
- [CONTRIBUTING.md](CONTRIBUTING.md): what needs agreement first, and why
- PyPI: https://pypi.org/project/sentience-governor/
- Website: https://getsentience.ai
