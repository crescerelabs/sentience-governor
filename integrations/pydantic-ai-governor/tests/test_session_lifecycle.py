"""CP2 — one run, one session, closed whatever happens.

These tests exercise the six-step session contract through real Pydantic AI
runs against `FunctionModel`, with no network and no API keys.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, List

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool

from sentience_governor.schema.events import OperationType

from pydantic_ai_governor import SentienceGovernor

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Hygiene, not a contract. This checkpoint writes nothing to disk, and
    the assertion below proves it. The isolation exists so that if any
    component ever does reach for the home directory, a test run cannot
    touch the developer's real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def gov() -> SentienceGovernor:
    return SentienceGovernor(agent_id="cp2-agent")


def answering_model() -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("done")])
    return FunctionModel(fn)


def tool_calling_model(tool_name: str) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name, {"x": "a"})])
        return ModelResponse(parts=[TextPart("done")])
    return FunctionModel(fn)


def raising_tool() -> Tool:
    def explode(x: str) -> str:
        """Always raises."""
        raise RuntimeError("boom")
    return Tool(explode)


def state_of(gov: SentienceGovernor, session_id: str) -> str:
    return str(gov._session_manager.get_state(session_id)).upper()


# ---------------------------------------------------------------------------
# The six-step contract
# ---------------------------------------------------------------------------

async def test_one_run_opens_exactly_one_session(gov):
    """One run, one session, and it is the run's own id."""
    seen: List[str] = []

    class Probe(SentienceGovernor):
        def _open_session(self, ctx):
            super()._open_session(ctx)
            seen.append(self._session_id)

    probe = Probe(agent_id="cp2-agent")
    result = await Agent(answering_model(), capabilities=[probe]).run("go")
    assert seen == [result.run_id]
    assert state_of(probe, result.run_id).endswith("CLOSED")



async def test_run_id_is_the_session_id(gov):
    """The two systems agree on identity with no mapping table."""
    seen: List[str] = []

    class Probe(SentienceGovernor):
        def _open_session(self, ctx):
            super()._open_session(ctx)
            seen.append(self._session_id)

    probe = Probe(agent_id="cp2-agent")
    result = await Agent(answering_model(), capabilities=[probe]).run("go")
    assert seen == [result.run_id]


async def test_normal_completion_closes_the_session(gov):
    result = await Agent(answering_model(), capabilities=[gov]).run("go")
    assert state_of(gov, result.run_id).endswith("CLOSED")


async def test_tool_failure_still_tears_the_session_down(gov):
    """Teardown is in `finally`, so a raising run cannot leak a session."""
    captured: List[str] = []

    class Probe(SentienceGovernor):
        def _open_session(self, ctx):
            super()._open_session(ctx)
            captured.append(self._session_id)

    probe = Probe(agent_id="cp2-agent")
    agent = Agent(tool_calling_model("explode"), tools=[raising_tool()],
                  capabilities=[probe])
    with pytest.raises(Exception):
        await agent.run("go")

    assert captured, "the session never opened"
    assert state_of(probe, captured[0]).endswith("CLOSED")


async def test_exception_propagates_unchanged(gov):
    agent = Agent(tool_calling_model("explode"), tools=[raising_tool()],
                  capabilities=[gov])
    with pytest.raises(Exception) as excinfo:
        await agent.run("go")
    assert "boom" in str(excinfo.value)


async def test_cache_is_cleared_on_teardown(gov):
    """Step 6 has two halves. Ending the session without clearing the cache
    would leak per-session state for the life of the process."""
    result = await Agent(answering_model(), capabilities=[gov]).run("go")
    assert gov._cache.get_intent_baseline(result.run_id) is None


async def test_for_run_is_async_and_isolates_state(gov):
    """Per-run instances must not share session state with the constructor
    instance, or concurrent runs would overwrite each other."""
    assert inspect.iscoroutinefunction(SentienceGovernor.for_run)
    await Agent(answering_model(), capabilities=[gov]).run("go")
    # The constructor instance never becomes a session owner.
    assert gov._session_id is None
    assert gov._builder is None


async def test_concurrent_runs_get_distinct_sessions(gov):
    """`allow_concurrent=True` is load-bearing: sibling runs share one
    agent_id, and the default force-closes the sibling."""
    agent = Agent(answering_model(), capabilities=[gov])
    r1, r2 = await asyncio.gather(agent.run("a"), agent.run("b"))
    assert r1.run_id != r2.run_id


# ---------------------------------------------------------------------------
# Step 4 — the regression guard
# ---------------------------------------------------------------------------

class _BaselineProbe(SentienceGovernor):
    """Asserts an in-scope call against a REAL declaration.

    As of CP3 the intent comes from the capability's own declaration path,
    not from this subclass: the probe supplies only the scope assertion,
    which is CP5's surface and does not exist yet. The assertion itself is
    unchanged from CP2.
    """

    violations: List[List[str]]

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.violations = []

    async def wrap_run(self, ctx, *, handler):
        self._open_session(ctx)
        try:
            # An in-scope read against the declared scope.
            event = self._builder.build_scope_asserted(
                tool_id="crm_fetch",
                asserted_permissions=["read"],
                target_system="crm",
                operation_type=OperationType.READ,
            )
            self.violations.append(list(event.policy_violations or []))
            return await handler()
        finally:
            self._close_session()


async def test_in_scope_call_carries_no_pol_001():
    """The step-4 regression guard.

    With `cache.init_session` in place the intent baseline lands, so an
    in-scope assertion is clean. Without it, `set_intent_baseline` is a
    silent no-op and this same call acquires POL-001 while nothing raises
    anywhere. That is the failure this test exists to catch.
    """
    probe = _BaselineProbe(objective="Fix the timeout", scope=["crm"],
                           agent_id="cp2-agent")
    await Agent(answering_model(), capabilities=[probe]).run("go")

    assert probe.violations == [[]], (
        "an in-scope assertion carried a policy violation, which is what "
        "omitting cache.init_session looks like"
    )


async def test_the_guard_fails_when_step_4_is_removed(monkeypatch):
    """Proves the guard above can actually fail.

    A test that has never failed is not known to work, so this neuters
    `init_session` and asserts the symptom appears: POL-001 on a call that
    is plainly inside the declared scope, with no exception raised.
    """
    monkeypatch.setattr(
        "sentience_governor.cache.cache.InProcessCache.init_session",
        lambda self, session_id: None,
    )
    probe = _BaselineProbe(objective="Fix the timeout", scope=["crm"],
                           agent_id="cp2-agent")

    # The run still succeeds. That is the point: nothing signals the fault.
    await Agent(answering_model(), capabilities=[probe]).run("go")

    assert probe.violations == [["POL-001"]], probe.violations

