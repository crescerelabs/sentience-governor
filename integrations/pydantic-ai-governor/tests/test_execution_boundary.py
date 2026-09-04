"""CP5 — the execution boundary: assert, dispatch, snapshot.

Ordering is the evidence here, so most of these tests assert on the shape
and sequence of what reached the trace rather than on a return value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest
from pydantic_ai import (Agent, DeferredToolRequests, DeferredToolResults,
                         ModelRetry)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import Tool

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


@pytest.fixture
def gov() -> SentienceGovernor:
    return SentienceGovernor(objective="Fix the timeout", scope=["crm"],
                             agent_id="cp5-agent")


def events(home: Path, session_id: str) -> List[dict]:
    path = home / ".sentience" / "traces" / "pydantic-ai" / f"{session_id}.jsonl"
    return ([json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            if path.exists() else [])


def types_of(evs: List[dict]) -> List[str]:
    return [e["event_type"] for e in evs]


def of_type(evs: List[dict], t: str) -> List[dict]:
    return [e for e in evs if e["event_type"] == t]


def crm_tool() -> Tool:
    def crm_fetch(customer: str) -> str:
        """Fetch a customer record."""
        return f"record:{customer}"
    return Tool(crm_fetch, metadata=CRM_READ)


def raising_tool() -> Tool:
    def explode(x: str) -> str:
        """Always raises."""
        raise RuntimeError("boom")
    return Tool(explode, metadata=CRM_READ)


def one_call(tool_name: str, arg: str, value: Any = "c1") -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name, {arg: value})])
        return ModelResponse(parts=[TextPart("done")])
    return FunctionModel(fn)


# ---------------------------------------------------------------------------
# The golden shape
# ---------------------------------------------------------------------------

async def test_assert_then_execute_then_snapshot(gov, isolated_home):
    result = await Agent(one_call("crm_fetch", "customer"), tools=[crm_tool()],
                         capabilities=[gov]).run("go")
    assert types_of(events(isolated_home, result.run_id)) == [
        "AGENT_REGISTERED", "INTENT_DECLARED", "SCOPE_ASSERTED",
        "CONTEXT_SNAPSHOT",
    ]


async def test_assertion_carries_the_resolved_classification(gov, isolated_home):
    result = await Agent(one_call("crm_fetch", "customer"), tools=[crm_tool()],
                         capabilities=[gov]).run("go")
    [scope] = of_type(events(isolated_home, result.run_id), "SCOPE_ASSERTED")
    assert scope["payload"]["tool_id"] == "crm_fetch"
    assert scope["payload"]["target_system"] == "crm"
    assert scope["payload"]["operation_type"] == "READ"
    assert scope["payload"]["asserted_permissions"] == ["read"]
    # In-scope against the declared scope, so no violation.
    assert scope.get("policy_violations") == []


async def test_tool_use_id_joins_the_assertion_to_its_snapshot(
    gov, isolated_home
):
    result = await Agent(one_call("crm_fetch", "customer"), tools=[crm_tool()],
                         capabilities=[gov]).run("go")
    evs = events(isolated_home, result.run_id)
    [scope] = of_type(evs, "SCOPE_ASSERTED")
    [snapshot] = of_type(evs, "CONTEXT_SNAPSHOT")
    assert scope["payload"]["tool_use_id"]
    assert scope["payload"]["tool_use_id"] == snapshot["payload"]["tool_use_id"]


async def test_undeclared_operation_uses_the_compatibility_fallback(
    isolated_home,
):
    """An unclassified call still asserts. The internal semantic is
    UNKNOWN; core cannot serialize that, so it maps to READ with no
    permissions. **That READ is compatibility, not a claim the tool read
    anything.** Nothing is inferred from the tool's name."""
    def db_delete_record(row: str) -> str:
        """Delete a row."""
        return "deleted"

    gov = SentienceGovernor(objective="o", scope=["crm"])
    result = await Agent(one_call("db_delete_record", "row"),
                         tools=[Tool(db_delete_record)],
                         capabilities=[gov]).run("go")
    [scope] = of_type(events(isolated_home, result.run_id), "SCOPE_ASSERTED")
    assert scope["payload"]["operation_type"] == "READ"
    assert scope["payload"]["asserted_permissions"] == []
    # The name said delete. The evidence does not.
    assert scope["payload"]["target_system"] == "db_delete_record"


async def test_the_fallback_does_not_manufacture_a_policy_signal(
    isolated_home,
):
    """The reason READ was chosen. EXECUTE is mutating, so it would attach
    SCOPE_OPERATION_UNEXPECTED and POL-001 to an undeclared session on the
    strength of a fallback rather than anything the developer did."""
    def mystery(x: str) -> str:
        """Unclassified."""
        return "r"

    gov = SentienceGovernor()          # no objective: undeclared session
    result = await Agent(one_call("mystery", "x"), tools=[Tool(mystery)],
                         capabilities=[gov]).run("go")
    [scope] = of_type(events(isolated_home, result.run_id), "SCOPE_ASSERTED")
    assert "SCOPE_OPERATION_UNEXPECTED" not in (scope.get("advisory_flags") or [])


async def test_declared_read_and_the_fallback_are_distinguishable(
    isolated_home,
):
    """Same operation_type, different permissions. The only marker."""
    gov = SentienceGovernor(objective="o", scope=["crm"])
    declared = await Agent(one_call("crm_fetch", "customer"),
                           tools=[crm_tool()], capabilities=[gov]).run("go")

    def plain(x: str) -> str:
        """Unclassified."""
        return "r"

    gov2 = SentienceGovernor(objective="o", scope=["crm"])
    unknown = await Agent(one_call("plain", "x"), tools=[Tool(plain)],
                          capabilities=[gov2]).run("go")

    [a] = of_type(events(isolated_home, declared.run_id), "SCOPE_ASSERTED")
    [b] = of_type(events(isolated_home, unknown.run_id), "SCOPE_ASSERTED")
    assert a["payload"]["operation_type"] == b["payload"]["operation_type"] == "READ"
    assert a["payload"]["asserted_permissions"] == ["read"]
    assert b["payload"]["asserted_permissions"] == []


async def test_context_size_is_an_estimate_not_a_character_count(
    isolated_home,
):
    """The defect Rev 7 removed. A 400-character result must not report
    400, and must match core's own estimator."""
    import json as _json

    long_value = "x" * 400

    def big(x: str) -> str:
        """Returns a long string."""
        return long_value

    gov = SentienceGovernor(objective="o", scope=["crm"])
    result = await Agent(one_call("big", "x"), tools=[Tool(big)],
                         capabilities=[gov]).run("go")
    [snap] = of_type(events(isolated_home, result.run_id), "CONTEXT_SNAPSHOT")
    size = snap["payload"]["context_size_tokens"]

    assert size == max(1, len(_json.dumps(long_value)) // 4)
    assert size != len(long_value)


# ---------------------------------------------------------------------------
# Failure: assertion without snapshot
# ---------------------------------------------------------------------------

async def test_raised_tool_asserts_but_never_snapshots(gov, isolated_home):
    """The absence of a snapshot is the outcome signal this schema has.
    There is no execution-outcome field, and none is invented."""
    agent = Agent(one_call("explode", "x"), tools=[raising_tool()],
                  capabilities=[gov])
    with pytest.raises(Exception):
        await agent.run("go")

    evs = [e for e in
           (Path(isolated_home) / ".sentience" / "traces" / "pydantic-ai"
            ).glob("*.jsonl")]
    assert evs, "no trace was written"
    written = [json.loads(l) for l in evs[0].read_text().splitlines() if l.strip()]
    assert len(of_type(written, "SCOPE_ASSERTED")) == 1
    assert of_type(written, "CONTEXT_SNAPSHOT") == []


async def test_exception_propagates_untouched(gov):
    agent = Agent(one_call("explode", "x"), tools=[raising_tool()],
                  capabilities=[gov])
    with pytest.raises(Exception) as excinfo:
        await agent.run("go")
    assert "boom" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Validation, retries, streaming
# ---------------------------------------------------------------------------

async def test_validation_failure_emits_no_assertion(gov, isolated_home):
    """Pydantic never routes a validation-failed call to this hook, so the
    absence needs no detection here. Pinned because the guarantee is
    Pydantic's, not ours, and could change."""
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[
                ToolCallPart("crm_fetch", {"customer": 123})])   # wrong type
        if calls["n"] == 2:
            return ModelResponse(parts=[
                ToolCallPart("crm_fetch", {"customer": "ok"})])
        return ModelResponse(parts=[TextPart("done")])

    result = await Agent(FunctionModel(fn), tools=[crm_tool()],
                         capabilities=[gov]).run("go")
    evs = events(isolated_home, result.run_id)
    assert len(of_type(evs, "SCOPE_ASSERTED")) == 1
    assert len(of_type(evs, "CONTEXT_SNAPSHOT")) == 1


async def test_retry_asserts_once_per_dispatched_attempt(gov, isolated_home):
    """Each retry really did dispatch, so each earns an assertion. Only the
    attempt that returned earns a snapshot."""
    state = {"n": 0}

    def flaky(x: str) -> str:
        """Retries once."""
        state["n"] += 1
        if state["n"] == 1:
            raise ModelRetry("try again")
        return "ok"

    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] <= 2:
            return ModelResponse(parts=[ToolCallPart("flaky", {"x": "a"})])
        return ModelResponse(parts=[TextPart("done")])

    result = await Agent(FunctionModel(fn),
                         tools=[Tool(flaky, metadata=CRM_READ)],
                         capabilities=[gov]).run("go")
    evs = events(isolated_home, result.run_id)
    assert len(of_type(evs, "SCOPE_ASSERTED")) == 2
    assert len(of_type(evs, "CONTEXT_SNAPSHOT")) == 1


async def test_streaming_preserves_the_same_shape(gov, isolated_home):
    agent = Agent(TestModel(), tools=[crm_tool()], capabilities=[gov])
    async with agent.run_stream("go") as stream:
        await stream.get_output()

    traces = list((Path(isolated_home) / ".sentience" / "traces"
                   / "pydantic-ai").glob("*.jsonl"))
    assert len(traces) == 1
    written = [json.loads(l) for l in traces[0].read_text().splitlines()
               if l.strip()]
    assert types_of(written) == ["AGENT_REGISTERED", "INTENT_DECLARED",
                                 "SCOPE_ASSERTED", "CONTEXT_SNAPSHOT"]


# ---------------------------------------------------------------------------
# Deferral and resumption
# ---------------------------------------------------------------------------

async def test_deferral_emits_no_assertion_and_resume_is_its_own_session(
    gov, isolated_home
):
    """A deferred call never dispatched, so nothing is asserted for it. The
    resumed run is a new Pydantic run and therefore a new Governor session:
    this integration introduces no cross-run correlation, and the two are
    deliberately not merged."""
    def approve_me(x: str) -> str:
        """Needs approval."""
        return f"did:{x}"

    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[ToolCallPart("approve_me", {"x": "a"})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(fn),
                  tools=[Tool(approve_me, requires_approval=True,
                              metadata=CRM_READ)],
                  output_type=[str, DeferredToolRequests],
                  capabilities=[gov])

    first = await agent.run("go")
    assert isinstance(first.output, DeferredToolRequests)
    assert of_type(events(isolated_home, first.run_id), "SCOPE_ASSERTED") == []

    call_id = first.output.approvals[0].tool_call_id
    second = await agent.run(
        message_history=first.all_messages(),
        deferred_tool_results=DeferredToolResults(approvals={call_id: True}),
    )

    assert second.run_id != first.run_id
    resumed = events(isolated_home, second.run_id)
    assert len(of_type(resumed, "SCOPE_ASSERTED")) == 1
    assert len(of_type(resumed, "CONTEXT_SNAPSHOT")) == 1
    # Two sessions, two files, not one merged record.
    assert {e["session_id"] for e in resumed} == {second.run_id}


# ---------------------------------------------------------------------------
# Non-interference and analyzer compatibility
# ---------------------------------------------------------------------------

async def test_the_boundary_does_not_alter_the_run(gov):
    plain = await Agent(one_call("crm_fetch", "customer"),
                        tools=[crm_tool()]).run("go")
    governed = await Agent(one_call("crm_fetch", "customer"),
                           tools=[crm_tool()], capabilities=[gov]).run("go")
    assert plain.output == governed.output
    assert len(plain.all_messages()) == len(governed.all_messages())


async def test_the_trace_is_consumed_by_the_shipped_analyzers(
    gov, isolated_home
):
    result = await Agent(one_call("crm_fetch", "customer"), tools=[crm_tool()],
                         capabilities=[gov]).run("go")
    from sentience_governor.analyze.pulse import compute_pulse

    pulse = compute_pulse(events(isolated_home, result.run_id))
    assert isinstance(pulse, dict) and pulse.get("status")
