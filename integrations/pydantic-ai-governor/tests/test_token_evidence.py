"""CP6 — token and model evidence.

Per-turn attribution is the property under test throughout: a run with
several model turns must produce one snapshot per turn, each carrying its
own numbers. A design that reads `ctx.usage` passes a single-turn test and
fails every one of these, which is why the lag is asserted directly rather
than left as a comment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool
from pydantic_ai.usage import RequestUsage

from pydantic_ai_governor import SentienceGovernor, evidence

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
                             agent_id="cp6-agent")


def events(home: Path, session_id: str) -> List[dict]:
    path = home / ".sentience" / "traces" / "pydantic-ai" / f"{session_id}.jsonl"
    return ([json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            if path.exists() else [])


def token_snapshots(evs: List[dict]) -> List[dict]:
    """CONTEXT_SNAPSHOTs that carry token accounting.

    The tool-result snapshots CP5 emits are the same event type, so the
    discriminator is `llm_turn_id`, which only the token path sets.
    """
    return [e for e in evs
            if e["event_type"] == "CONTEXT_SNAPSHOT"
            and e["payload"].get("llm_turn_id") is not None]


def crm_fetch(account: str) -> str:
    return f"account {account}: ok"


CRM_TOOL = Tool(crm_fetch, metadata=CRM_READ)


def two_turn_model(
    *,
    first: RequestUsage,
    second: RequestUsage,
    response_ids: tuple = (None, None),
) -> FunctionModel:
    """A model that calls one tool, then answers. Two turns, two usages."""
    async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(getattr(p, "part_kind", None) == "tool-return"
                   for m in messages for p in m.parts):
            return ModelResponse(
                parts=[ToolCallPart(tool_name="crm_fetch",
                                    args={"account": "A1"},
                                    tool_call_id="call-1")],
                usage=first,
                model_name="test-model",
                provider_name="test-provider",
                provider_response_id=response_ids[0],
            )
        return ModelResponse(
            parts=[TextPart(content="done")],
            usage=second,
            model_name="test-model",
            provider_name="test-provider",
            provider_response_id=response_ids[1],
        )

    return FunctionModel(fn)


# --- per-turn attribution ------------------------------------------------

async def test_each_turn_gets_its_own_snapshot_with_its_own_numbers(
        gov, isolated_home):
    """The acceptance criterion: no snapshot carries another turn's numbers."""
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    snaps = token_snapshots(events(isolated_home, result.run_id))
    assert len(snaps) == 2, "one snapshot per model turn"

    pairs = [(s["payload"]["llm_prompt_tokens"],
              s["payload"]["llm_completion_tokens"]) for s in snaps]
    assert pairs == [(10, 5), (30, 7)]


async def test_turn_usage_sums_to_the_run_total(gov, isolated_home):
    """Acceptance: the parts add up to what the framework says the run cost."""
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    snaps = token_snapshots(events(isolated_home, result.run_id))
    total = result.usage
    assert sum(s["payload"]["llm_prompt_tokens"] for s in snaps) == \
        total.input_tokens
    assert sum(s["payload"]["llm_completion_tokens"] for s in snaps) == \
        total.output_tokens


async def test_ctx_usage_lags_and_is_therefore_not_the_source(gov, isolated_home):
    """The superseded design, proven wrong rather than asserted wrong.

    If a future change reads `ctx.usage` at this hook, the recorded values
    below stop matching the emitted snapshots and this test fails. That is
    the whole point of measuring the lag here instead of describing it.
    """
    seen: List[tuple] = []

    class Watching(SentienceGovernor):
        async def after_model_request(self, ctx, *, request_context, response):
            seen.append((ctx.usage.input_tokens, ctx.usage.output_tokens,
                         response.usage.input_tokens,
                         response.usage.output_tokens))
            return await super().after_model_request(
                ctx, request_context=request_context, response=response)

    watcher = Watching(objective="Fix the timeout", scope=["crm"],
                       agent_id="cp6-agent")
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[watcher])
    result = await agent.run("go")

    ctx_pairs = [(a, b) for a, b, _, _ in seen]
    response_pairs = [(c, d) for _, _, c, d in seen]

    assert response_pairs == [(10, 5), (30, 7)], "the response is current"
    assert ctx_pairs == [(0, 0), (10, 5)], "ctx.usage lags by one request"
    assert ctx_pairs != response_pairs

    snaps = token_snapshots(events(isolated_home, result.run_id))
    emitted = [(s["payload"]["llm_prompt_tokens"],
                s["payload"]["llm_completion_tokens"]) for s in snaps]
    assert emitted == response_pairs, "we record the response, never ctx.usage"


# --- context_size_tokens is measured here, estimated in CP5 --------------

async def test_context_size_tokens_is_the_measured_input_count(
        gov, isolated_home):
    """Rev 8: measured `usage.input_tokens`, not the CP5 estimator."""
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    snaps = token_snapshots(events(isolated_home, result.run_id))
    sizes = [s["payload"]["context_size_tokens"] for s in snaps]
    assert sizes == [10, 30]
    # The duplication with llm_prompt_tokens is deliberate: one
    # measurement, two names. Nothing invents a difference.
    assert sizes == [s["payload"]["llm_prompt_tokens"] for s in snaps]


async def test_tool_result_snapshots_keep_the_cp5_estimate(gov, isolated_home):
    """The two snapshot kinds do not converge on one rule.

    A tool boundary has no measured model-input value, so CP5's estimator
    stays. This guards the boundary Rev 8 drew, in the direction that would
    otherwise rot silently.
    """
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    evs = events(isolated_home, result.run_id)
    tool_snaps = [e for e in evs
                  if e["event_type"] == "CONTEXT_SNAPSHOT"
                  and e["payload"].get("llm_turn_id") is None]
    assert len(tool_snaps) == 1
    assert tool_snaps[0]["payload"]["context_size_tokens"] == \
        evidence.estimate_context_tokens("account A1: ok")


# --- the tool_use_ids join ----------------------------------------------

async def test_snapshot_joins_to_the_tool_calls_that_turn_issued(
        gov, isolated_home):
    """The join is the reason the field exists, so it is tested as a join."""
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    evs = events(isolated_home, result.run_id)
    snaps = token_snapshots(evs)

    # The ids the assertions recorded for real dispatched calls...
    asserted = [e["payload"]["tool_use_id"]
                for e in evs if e["event_type"] == "SCOPE_ASSERTED"]
    assert asserted == ["call-1"]

    # ...are reachable from the token snapshot for the turn that issued
    # them, which is what an analyzer does to attribute burn.
    by_id = {}
    for snap in snaps:
        for call_id in snap["payload"].get("tool_use_ids") or []:
            by_id[call_id] = snap
    assert set(by_id) == set(asserted)
    assert by_id["call-1"]["payload"]["llm_prompt_tokens"] == 10


async def test_tool_use_ids_preserve_response_order(gov, isolated_home):
    """Order is response order, and is not sorted or de-duplicated."""
    async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(getattr(p, "part_kind", None) == "tool-return"
                   for m in messages for p in m.parts):
            return ModelResponse(
                parts=[
                    ToolCallPart(tool_name="crm_fetch", args={"account": "z"},
                                 tool_call_id="call-z"),
                    ToolCallPart(tool_name="crm_fetch", args={"account": "a"},
                                 tool_call_id="call-a"),
                    ToolCallPart(tool_name="crm_fetch", args={"account": "m"},
                                 tool_call_id="call-m"),
                ],
                usage=RequestUsage(input_tokens=12, output_tokens=3),
                model_name="test-model",
                provider_name="test-provider",
            )
        return ModelResponse(
            parts=[TextPart(content="done")],
            usage=RequestUsage(input_tokens=40, output_tokens=2),
            model_name="test-model",
            provider_name="test-provider",
        )

    agent = Agent(FunctionModel(fn), tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    snaps = token_snapshots(events(isolated_home, result.run_id))
    assert snaps[0]["payload"]["tool_use_ids"] == ["call-z", "call-a", "call-m"]


async def test_a_turn_with_no_tool_calls_omits_the_field(gov, isolated_home):
    """Absence, not an empty list.

    Core omits None and preserves what is present, so `[]` would assert
    "this turn issued exactly zero tools" on every text-only turn.
    """
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    snaps = token_snapshots(events(isolated_home, result.run_id))
    assert snaps[0]["payload"]["tool_use_ids"] == ["call-1"]
    assert "tool_use_ids" not in snaps[1]["payload"]


def test_ids_are_read_off_parts_and_never_synthesized():
    """A part with no usable id contributes nothing rather than a guess."""
    class Bare:
        part_kind = "tool-call"
        tool_call_id = None

    class Response:
        parts = [Bare()]

    assert evidence.issued_tool_use_ids(Response()) is None


# --- cached tokens -------------------------------------------------------

async def test_cache_usage_maps_straight_through(gov, isolated_home):
    """Direct mapping only, never estimated or recomputed."""
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5,
                           cache_read_tokens=8, cache_write_tokens=2),
        second=RequestUsage(input_tokens=30, output_tokens=7,
                            cache_read_tokens=0, cache_write_tokens=0),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    snaps = token_snapshots(events(isolated_home, result.run_id))
    assert snaps[0]["payload"]["llm_cached_read_tokens"] == 8
    assert snaps[0]["payload"]["llm_cached_write_tokens"] == 2
    # Reported zero is a measurement and core preserves it.
    assert snaps[1]["payload"]["llm_cached_read_tokens"] == 0
    assert snaps[1]["payload"]["llm_cached_write_tokens"] == 0


def test_unreported_usage_is_absent_rather_than_manufactured():
    """Nothing reported means the field goes away, not that it becomes 0."""
    class NoCacheFields:
        input_tokens = 11
        output_tokens = 4

    class Response:
        usage = NoCacheFields()
        parts: list = []
        model_name = "m"
        provider_name = "p"
        provider_response_id = None

    turn = evidence.read_turn(Response(), 0)
    assert turn.llm_prompt_tokens == 11
    assert turn.llm_cached_read_tokens is None
    assert turn.llm_cached_write_tokens is None


# --- identity and its provenance ----------------------------------------

async def test_provider_response_id_is_preferred_and_marked_as_such(
        gov, isolated_home):
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
        response_ids=("resp-aaa", "resp-bbb"),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    snaps = token_snapshots(events(isolated_home, result.run_id))
    assert [s["payload"]["llm_turn_id"] for s in snaps] == \
        ["resp-aaa", "resp-bbb"]
    for snap in snaps:
        assert snap["payload"]["provenance"] == [evidence.PROVIDER_TURN_ID]


async def test_absent_provider_id_falls_back_and_is_marked_local(
        gov, isolated_home):
    """The fallback is usable, and is never presented as provider-issued."""
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    snaps = token_snapshots(events(isolated_home, result.run_id))
    ids = [s["payload"]["llm_turn_id"] for s in snaps]
    assert all(i.startswith("run_step:") for i in ids), ids
    assert len(set(ids)) == 2, "turns stay distinguishable under the fallback"
    for snap in snaps:
        assert snap["payload"]["provenance"] == [evidence.LOCAL_TURN_ID]
        assert evidence.PROVIDER_TURN_ID not in snap["payload"]["provenance"]


def test_both_branches_are_labelled_positively():
    """Provider-issued is a claim we make, not one inferred from silence."""
    class WithId:
        provider_response_id = "resp-1"

    class WithoutId:
        provider_response_id = None

    provider_id, provider_prov = evidence.turn_identity(WithId(), 3)
    local_id, local_prov = evidence.turn_identity(WithoutId(), 3)

    assert (provider_id, provider_prov) == ("resp-1", [evidence.PROVIDER_TURN_ID])
    assert (local_id, local_prov) == ("run_step:3", [evidence.LOCAL_TURN_ID])
    assert provider_prov and local_prov, "neither branch signals by absence"


# --- what a token snapshot must not carry -------------------------------

async def test_token_snapshots_carry_no_flags_and_no_violations(
        gov, isolated_home):
    """The builder does not run these through `_eval_context`, by design.

    A token snapshot observes no data classification, so evaluating it
    would manufacture a POL-003 on every model turn.
    """
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    for snap in token_snapshots(events(isolated_home, result.run_id)):
        assert snap["advisory_flags"] == []
        assert snap["policy_violations"] == []


async def test_identity_is_present_and_taken_from_the_response(
        gov, isolated_home):
    """Identity is whatever the framework stamped, not our own label.

    Asserted against the run's own message history rather than a literal,
    because the framework overwrites `model_name` on the response it
    returns. Reading the response is therefore the only way to record the
    authoritative identity, and a hardcoded expectation here would hide a
    regression where we started inventing one.
    """
    model = two_turn_model(
        first=RequestUsage(input_tokens=10, output_tokens=5),
        second=RequestUsage(input_tokens=30, output_tokens=7),
    )
    agent = Agent(model, tools=[CRM_TOOL], capabilities=[gov])
    result = await agent.run("go")

    responses = [m for m in result.all_messages()
                 if isinstance(m, ModelResponse)]
    authoritative = [(r.model_name, r.provider_name) for r in responses]
    assert all(name for name, _ in authoritative), "identity is present"

    snaps = token_snapshots(events(isolated_home, result.run_id))
    recorded = [(s["payload"].get("model_identifier"),
                 s["payload"].get("provider")) for s in snaps]
    assert recorded == authoritative


async def test_the_response_is_returned_unchanged(gov, isolated_home):
    """A reader, not a filter: same output, same messages, same usage."""
    def build() -> FunctionModel:
        return two_turn_model(
            first=RequestUsage(input_tokens=10, output_tokens=5),
            second=RequestUsage(input_tokens=30, output_tokens=7),
        )

    governed = await Agent(build(), tools=[CRM_TOOL],
                           capabilities=[gov]).run("go")
    plain = await Agent(build(), tools=[CRM_TOOL]).run("go")

    assert governed.output == plain.output
    assert len(governed.all_messages()) == len(plain.all_messages())
    assert governed.usage.input_tokens == plain.usage.input_tokens
    assert governed.usage.output_tokens == plain.usage.output_tokens
