"""CP7 — concurrency, isolation and error paths.

Two properties are under test, and they are not the same property.

**Isolation**: concurrent runs and parallel tool calls must not contaminate
each other's evidence.

**Non-interference**: the agent must behave identically with the capability
attached and without it. This is about the *agent*, not about silence. D1
and D2 deliberately warn and emit a `GOVERNANCE_ERROR`; visible fail-open is
additive evidence plus a warning, never interference. So every
non-interference test below compares agent-visible behavior — output,
message count, usage — and never asserts that nothing was said.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pydantic_ai import (Agent, DeferredToolRequests, DeferredToolResults,
                         ModelRetry)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import Tool
from pydantic_ai.usage import RequestUsage

from pydantic_ai_governor import SentienceGovernor

pytestmark = pytest.mark.anyio

CRM_READ = {"sentience_governor": {
    "operation": "READ", "target_system": "crm", "classification": ["internal"]}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def gov(**kw: Any) -> SentienceGovernor:
    kw.setdefault("objective", "Fix the timeout")
    kw.setdefault("scope", ["crm"])
    kw.setdefault("agent_id", "cp7-agent")
    return SentienceGovernor(**kw)


def events(home: Path, session_id: str) -> List[dict]:
    path = home / ".sentience" / "traces" / "pydantic-ai" / f"{session_id}.jsonl"
    return ([json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            if path.exists() else [])


def all_traces(home: Path) -> Dict[str, List[dict]]:
    base = home / ".sentience" / "traces" / "pydantic-ai"
    return {p.stem: [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
            for p in sorted(base.glob("*.jsonl"))} if base.exists() else {}


def of_type(evs: List[dict], t: str) -> List[dict]:
    return [e for e in evs if e["event_type"] == t]


def crm_fetch(account: str) -> str:
    return f"account {account}: ok"


def crm_tool(metadata: Any = CRM_READ) -> Tool:
    return Tool(crm_fetch, metadata=metadata)


def one_call_model(account: str = "A1") -> FunctionModel:
    async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(getattr(p, "part_kind", None) == "tool-return"
                   for m in messages for p in m.parts):
            return ModelResponse(
                parts=[ToolCallPart(tool_name="crm_fetch",
                                    args={"account": account},
                                    tool_call_id="call-1")],
                usage=RequestUsage(input_tokens=10, output_tokens=5),
                model_name="m", provider_name="p")
        return ModelResponse(parts=[TextPart(content="done")],
                             usage=RequestUsage(input_tokens=30, output_tokens=7),
                             model_name="m", provider_name="p")
    return FunctionModel(fn)


def parallel_call_model(ids: tuple = ("call-a", "call-b", "call-c")) -> FunctionModel:
    """One model response issuing several tool calls at once."""
    async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(getattr(p, "part_kind", None) == "tool-return"
                   for m in messages for p in m.parts):
            return ModelResponse(
                parts=[ToolCallPart(tool_name="crm_fetch",
                                    args={"account": f"A{n}"},
                                    tool_call_id=call_id)
                       for n, call_id in enumerate(ids)],
                usage=RequestUsage(input_tokens=12, output_tokens=6),
                model_name="m", provider_name="p")
        return ModelResponse(parts=[TextPart(content="done")],
                             usage=RequestUsage(input_tokens=40, output_tokens=2),
                             model_name="m", provider_name="p")
    return FunctionModel(fn)


def answering_model() -> FunctionModel:
    async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="done")],
                             usage=RequestUsage(input_tokens=9, output_tokens=3),
                             model_name="m", provider_name="p")
    return FunctionModel(fn)


# ---------------------------------------------------------------------------
# Isolation — concurrent runs
# ---------------------------------------------------------------------------

async def test_concurrent_runs_are_two_isolated_sessions(isolated_home):
    """Sibling runs share one agent_id; each still gets its own evidence."""
    agent = Agent(one_call_model(), tools=[crm_tool()], capabilities=[gov()])
    r1, r2 = await asyncio.gather(agent.run("a"), agent.run("b"))

    assert r1.run_id != r2.run_id
    traces = all_traces(isolated_home)
    assert set(traces) == {r1.run_id, r2.run_id}, "one trace per run, no bleed"

    for run_id in (r1.run_id, r2.run_id):
        evs = traces[run_id]
        assert {e["session_id"] for e in evs} == {run_id}
        assert len(of_type(evs, "SCOPE_ASSERTED")) == 1
        assert len(of_type(evs, "AGENT_REGISTERED")) == 1


async def test_concurrent_runs_emit_no_governance_error(isolated_home, capsys):
    """`allow_concurrent=True` is what keeps this quiet.

    Without it the second `session_start` force-closes the sibling. Core
    routes GOVERNANCE_ERROR to stdout regardless of sink, so stdout is
    where the absence has to be proven.
    """
    agent = Agent(one_call_model(), tools=[crm_tool()], capabilities=[gov()])
    r1, r2 = await asyncio.gather(agent.run("a"), agent.run("b"))

    captured = capsys.readouterr()
    assert "GOVERNANCE_ERROR" not in captured.out
    assert "GOVERNANCE_ERROR" not in captured.err
    for run_id in (r1.run_id, r2.run_id):
        assert of_type(events(isolated_home, run_id), "GOVERNANCE_ERROR") == []


async def test_sequence_numbers_are_per_session_and_strictly_increasing(
        isolated_home):
    """Per-session, not global: two runs each start their own count."""
    agent = Agent(one_call_model(), tools=[crm_tool()], capabilities=[gov()])
    r1, r2 = await asyncio.gather(agent.run("a"), agent.run("b"))

    for run_id in (r1.run_id, r2.run_id):
        seqs = [e["event_sequence_number"]
                for e in events(isolated_home, run_id)]
        assert len(seqs) == len(set(seqs)), "unique within the session"
        assert seqs == sorted(seqs), "strictly increasing"
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), "no gaps"


async def test_many_concurrent_runs_stay_separated(isolated_home):
    """Eight at once, because two can pass by luck of scheduling."""
    agent = Agent(one_call_model(), tools=[crm_tool()], capabilities=[gov()])
    results = await asyncio.gather(*(agent.run(f"q{i}") for i in range(8)))

    run_ids = [r.run_id for r in results]
    assert len(set(run_ids)) == 8
    traces = all_traces(isolated_home)
    assert set(traces) == set(run_ids)
    for run_id in run_ids:
        assert len(of_type(traces[run_id], "SCOPE_ASSERTED")) == 1


# ---------------------------------------------------------------------------
# Isolation — parallel tool calls inside one response
# ---------------------------------------------------------------------------

async def test_parallel_calls_carry_distinct_ids_on_both_events(isolated_home):
    """Assertions and snapshots must key to the same three calls."""
    ids = ("call-a", "call-b", "call-c")
    agent = Agent(parallel_call_model(ids), tools=[crm_tool()],
                  capabilities=[gov()])
    result = await agent.run("go")
    evs = events(isolated_home, result.run_id)

    asserted = [e["payload"]["tool_use_id"] for e in of_type(evs, "SCOPE_ASSERTED")]
    snapped = [e["payload"]["tool_use_id"] for e in of_type(evs, "CONTEXT_SNAPSHOT")
               if e["payload"].get("tool_use_id") is not None]

    assert len(asserted) == 3 and len(set(asserted)) == 3, "distinct"
    assert set(asserted) == set(ids)
    assert set(snapped) == set(asserted), "identical id sets between them"


async def test_parallel_calls_do_not_cross_contaminate_classification(
        isolated_home):
    """The evidence for each call must describe that call.

    With per-call state on `self`, three concurrent calls would race and
    some assertions would carry another call's resolution. Here each is
    checked against the tool it actually names.
    """
    agent = Agent(parallel_call_model(), tools=[crm_tool()],
                  capabilities=[gov()])
    result = await agent.run("go")

    for event in of_type(events(isolated_home, result.run_id), "SCOPE_ASSERTED"):
        assert event["payload"]["tool_id"] == "crm_fetch"
        assert event["payload"]["target_system"] == "crm"
        assert event["payload"]["operation_type"] == "READ"
        assert event["payload"]["asserted_permissions"] == ["read"]


# ---------------------------------------------------------------------------
# The acceptance criterion: no per-call state on `self`
# ---------------------------------------------------------------------------

class _AttributeWatcher(SentienceGovernor):
    """Snapshots `vars(self)` around every tool call.

    The static check CP7 asks for. Anything per-call written to `self`
    shows up as a key whose value changed between the entry to one call
    and the entry to the next.
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.snapshots: List[Dict[str, int]] = []

    def _snapshot(self) -> Dict[str, int]:
        # Identity, not equality: a mutated-in-place value is still a
        # per-call write, and `id()` catches a rebind that compares equal.
        return {k: id(v) for k, v in vars(self).items() if k != "snapshots"}

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        self.snapshots.append(self._snapshot())
        result = await super().wrap_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args, handler=handler)
        self.snapshots.append(self._snapshot())
        return result


async def test_capability_holds_no_per_call_attribute(isolated_home):
    """Three tool calls must leave the instance's attributes untouched.

    `for_run` clones by copying `__dict__`, so the clone that actually
    serves the run shares the `snapshots` list object with the instance
    read here: these are the run instance's writes.
    """
    watcher = _AttributeWatcher(objective="Fix the timeout", scope=["crm"],
                                agent_id="cp7-agent")
    agent = Agent(parallel_call_model(), tools=[crm_tool()],
                  capabilities=[watcher])
    await agent.run("go")

    seen = watcher.snapshots
    assert len(seen) == 6, "entry and exit for each of three parallel calls"

    first = seen[0]
    for snap in seen[1:]:
        assert set(snap) == set(first), "no attribute appeared or vanished"
        changed = {k for k in first if snap[k] != first[k]}
        assert changed == set(), f"per-call state on self: {sorted(changed)}"


def test_no_per_call_attribute_is_declared_in_the_constructor():
    """A second, cheaper guard that needs no run.

    The set is pinned so that adding per-call state is a deliberate act
    that fails here first, with a name to look at.
    """
    instance = gov()
    assert set(vars(instance)) == {
        "_agent_id", "_default", "_session_manager", "_cache",
        "_session_id", "_builder", "_sink", "_declaration", "_rejection",
    }


# ---------------------------------------------------------------------------
# Non-interference — the agent behaves identically
# ---------------------------------------------------------------------------

async def assert_identical(build_model, tools: List[Tool], *,
                           capability: Any, prompt: str = "go") -> None:
    """Run twice, with and without the capability, and compare."""
    governed = await Agent(build_model(), tools=tools,
                           capabilities=[capability]).run(prompt)
    plain = await Agent(build_model(), tools=tools).run(prompt)

    assert governed.output == plain.output
    assert len(governed.all_messages()) == len(plain.all_messages())
    assert governed.usage.input_tokens == plain.usage.input_tokens
    assert governed.usage.output_tokens == plain.usage.output_tokens


async def test_non_interference_on_a_normal_return(isolated_home):
    await assert_identical(one_call_model, [crm_tool()], capability=gov())


async def test_non_interference_on_a_text_only_run(isolated_home):
    await assert_identical(answering_model, [], capability=gov())


async def test_non_interference_on_parallel_calls(isolated_home):
    await assert_identical(parallel_call_model, [crm_tool()], capability=gov())


async def test_non_interference_on_a_retry(isolated_home):
    """A `ModelRetry` must still reach the model, unchanged."""
    calls: List[int] = []

    def flaky(account: str) -> str:
        calls.append(1)
        if len(calls) == 1:
            raise ModelRetry("try again")
        return "ok"

    def build():
        async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
            tool_returns = sum(
                1 for m in messages for p in m.parts
                if getattr(p, "part_kind", None) in ("tool-return", "retry-prompt"))
            if tool_returns < 2:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="flaky", args={"account": "A1"},
                                        tool_call_id=f"call-{tool_returns}")],
                    usage=RequestUsage(input_tokens=10, output_tokens=5),
                    model_name="m", provider_name="p")
            return ModelResponse(parts=[TextPart(content="done")],
                                 usage=RequestUsage(input_tokens=20, output_tokens=4),
                                 model_name="m", provider_name="p")
        return FunctionModel(fn)

    tool = Tool(flaky, metadata=CRM_READ)
    governed = await Agent(build(), tools=[tool],
                           capabilities=[gov()]).run("go")
    governed_calls = len(calls)
    calls.clear()
    plain = await Agent(build(), tools=[tool]).run("go")

    assert governed.output == plain.output
    assert len(governed.all_messages()) == len(plain.all_messages())
    assert governed_calls == len(calls), "the tool ran the same number of times"


async def test_non_interference_on_a_raised_tool(isolated_home):
    """The exception propagates untouched, same type and same message."""
    def explodes(account: str) -> str:
        raise RuntimeError("boom")

    tool = Tool(explodes, metadata=CRM_READ)

    def build():
        async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
            return ModelResponse(
                parts=[ToolCallPart(tool_name="explodes", args={"account": "A1"},
                                    tool_call_id="call-1")],
                usage=RequestUsage(input_tokens=10, output_tokens=5),
                model_name="m", provider_name="p")
        return FunctionModel(fn)

    with pytest.raises(RuntimeError) as governed:
        await Agent(build(), tools=[tool], capabilities=[gov()]).run("go")
    with pytest.raises(RuntimeError) as plain:
        await Agent(build(), tools=[tool]).run("go")

    assert str(governed.value) == str(plain.value) == "boom"
    assert type(governed.value) is type(plain.value)


async def test_non_interference_on_a_validation_failure(isolated_home):
    """A call the framework rejects never reaches the boundary, either way."""
    def build():
        async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
            bad = any(getattr(p, "part_kind", None) == "retry-prompt"
                      for m in messages for p in m.parts)
            if not bad:
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="crm_fetch",
                                        args={"wrong_field": 1},
                                        tool_call_id="call-1")],
                    usage=RequestUsage(input_tokens=10, output_tokens=5),
                    model_name="m", provider_name="p")
            return ModelResponse(parts=[TextPart(content="done")],
                                 usage=RequestUsage(input_tokens=20, output_tokens=4),
                                 model_name="m", provider_name="p")
        return FunctionModel(fn)

    await assert_identical(build, [crm_tool()], capability=gov())


async def test_non_interference_on_streaming(isolated_home):
    """Streamed output and the resulting message history both match.

    `TestModel` rather than `FunctionModel`, which needs a separate
    `stream_function` to serve a streamed request at all.
    """
    async with Agent(TestModel(), tools=[crm_tool()],
                     capabilities=[gov()]).run_stream("go") as governed:
        governed_out = await governed.get_output()
        governed_messages = len(governed.all_messages())

    async with Agent(TestModel(), tools=[crm_tool()]).run_stream("go") as plain:
        plain_out = await plain.get_output()
        plain_messages = len(plain.all_messages())

    assert governed_out == plain_out
    assert governed_messages == plain_messages


async def test_non_interference_on_deferral_and_resumption(isolated_home):
    """A deferred call, then a resume, behave identically."""
    def build():
        async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
            if not any(getattr(p, "part_kind", None) == "tool-return"
                       for m in messages for p in m.parts):
                return ModelResponse(
                    parts=[ToolCallPart(tool_name="needs_approval",
                                        args={"account": "A1"},
                                        tool_call_id="call-1")],
                    usage=RequestUsage(input_tokens=10, output_tokens=5),
                    model_name="m", provider_name="p")
            return ModelResponse(parts=[TextPart(content="done")],
                                 usage=RequestUsage(input_tokens=20, output_tokens=4),
                                 model_name="m", provider_name="p")
        return FunctionModel(fn)

    def needs_approval(account: str) -> str:
        return f"approved {account}"

    tool = Tool(needs_approval, metadata=CRM_READ, requires_approval=True)

    async def deferred_cycle(capabilities):
        agent = Agent(build(), tools=[tool],
                      output_type=[str, DeferredToolRequests],
                      capabilities=capabilities)
        first = await agent.run("go")
        assert isinstance(first.output, DeferredToolRequests)
        approvals = {c.tool_call_id: True for c in first.output.approvals}
        second = await agent.run(
            message_history=first.all_messages(),
            deferred_tool_results=DeferredToolResults(approvals=approvals))
        return first, second

    g_first, g_second = await deferred_cycle([gov()])
    p_first, p_second = await deferred_cycle([])

    assert g_second.output == p_second.output
    assert len(g_first.all_messages()) == len(p_first.all_messages())
    assert len(g_second.all_messages()) == len(p_second.all_messages())


async def test_non_interference_across_concurrent_runs(isolated_home):
    """Concurrency is where a shared mutable would show, so compare there."""
    capability = gov()
    agent = Agent(one_call_model(), tools=[crm_tool()],
                  capabilities=[capability])
    governed = await asyncio.gather(*(agent.run(f"q{i}") for i in range(4)))

    plain_agent = Agent(one_call_model(), tools=[crm_tool()])
    plain = await asyncio.gather(*(plain_agent.run(f"q{i}") for i in range(4)))

    assert [r.output for r in governed] == [r.output for r in plain]
    assert [len(r.all_messages()) for r in governed] == \
        [len(r.all_messages()) for r in plain]
    assert [r.usage.input_tokens for r in governed] == \
        [r.usage.input_tokens for r in plain]


# ---------------------------------------------------------------------------
# The D1 and D2 paths, proven non-interfering rather than assumed
# ---------------------------------------------------------------------------

async def test_d1_malformed_declaration_does_not_interfere(isolated_home):
    """A malformed run declaration warns and emits, and changes nothing."""
    capability = gov()
    bad_metadata = {"sentience_governor": {"objective": 42}}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        governed = await Agent(one_call_model(), tools=[crm_tool()],
                               capabilities=[capability]).run(
                                   "go", metadata=bad_metadata)
    plain = await Agent(one_call_model(), tools=[crm_tool()]).run(
        "go", metadata=bad_metadata)

    assert governed.output == plain.output
    assert len(governed.all_messages()) == len(plain.all_messages())
    assert governed.usage.input_tokens == plain.usage.input_tokens
    assert governed.usage.output_tokens == plain.usage.output_tokens
    # Non-interference is not silence: the warning is required behavior.
    assert any(issubclass(w.category, UserWarning) for w in caught)


async def test_d2_malformed_classification_does_not_interfere(isolated_home):
    """Same for a malformed tool classification."""
    bad_tool = crm_tool({"sentience_governor": {"operation": "read"}})

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        governed = await Agent(one_call_model(), tools=[bad_tool],
                               capabilities=[gov()]).run("go")
    plain = await Agent(one_call_model(), tools=[bad_tool]).run("go")

    assert governed.output == plain.output
    assert len(governed.all_messages()) == len(plain.all_messages())
    assert governed.usage.input_tokens == plain.usage.input_tokens
    assert governed.usage.output_tokens == plain.usage.output_tokens
    assert any(issubclass(w.category, UserWarning) for w in caught)


async def test_d2_under_parallel_calls_still_does_not_interfere(isolated_home):
    """The malformed path under concurrency, which is where it would bite."""
    bad_tool = crm_tool({"sentience_governor": {"operation": "read"}})

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        governed = await Agent(parallel_call_model(), tools=[bad_tool],
                               capabilities=[gov()]).run("go")
    plain = await Agent(parallel_call_model(), tools=[bad_tool]).run("go")

    assert governed.output == plain.output
    assert len(governed.all_messages()) == len(plain.all_messages())
    assert governed.usage.output_tokens == plain.usage.output_tokens


# ---------------------------------------------------------------------------
# `allow_concurrent=True` on every session_start
# ---------------------------------------------------------------------------

async def test_every_session_start_opts_into_concurrency(isolated_home,
                                                         monkeypatch):
    """Asserted on the call, because the trace cannot show it.

    Measured: flipping this flag to False changes nothing in the emitted
    evidence. Core force-closes the sibling session in its registry and
    logs `SESSION_FORCE_CLOSED`, but each run owns its own `EventBuilder`
    and `SinkWriter`, so both traces stay complete and identical either
    way. There is therefore no black-box guard to write — the flag is
    pinned where it is set, or not at all.

    It stays `True` regardless: sibling runs on one `Agent` genuinely
    share an `agent_id`, and relying on a force-close being harmless is a
    weaker position than not colliding in the first place.
    """
    from sentience_governor.session_manager.manager import SessionManager

    seen: List[Any] = []
    original = SessionManager.session_start

    def spy(self, *args, **kwargs):
        seen.append(kwargs.get("allow_concurrent"))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SessionManager, "session_start", spy)

    agent = Agent(one_call_model(), tools=[crm_tool()], capabilities=[gov()])
    await asyncio.gather(agent.run("a"), agent.run("b"))

    assert seen == [True, True], "every session_start opts in, by keyword"
