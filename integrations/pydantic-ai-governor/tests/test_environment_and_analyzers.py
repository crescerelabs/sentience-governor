"""CP8 — the supported environment, and what the shipped analyzers make of us.

Two properties that no other suite covers.

**Environment.** The distribution declares a Python floor and a
`pydantic-ai-slim` range. Those declarations are only worth what the CI
matrix actually exercises, so the assertions here are about the *running*
interpreter and the *installed* dependency, not about strings in
`pyproject.toml` — `test_packaging.py` already owns the strings.

**Analyzer compatibility.** A trace nothing can read is not evidence. The
shipped analyzers were written against Claude Code and MCP traces, so the
question this suite answers is whether they read a Pydantic AI session as a
session rather than as noise. Real assertions on real fields, not
`isinstance(result, dict)`.
"""

from __future__ import annotations

import json
import sys
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any, List

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool
from pydantic_ai.usage import RequestUsage

from sentience_governor.analyze.pulse import compute_pulse
from sentience_governor.analyze.undeclared_intent import (
    compute_undeclared_intent_spend)

from pydantic_ai_governor import SentienceGovernor

pytestmark = pytest.mark.anyio

CRM_READ = {"sentience_governor": {
    "operation": "READ", "target_system": "crm", "classification": ["internal"]}}

#: The declared floor, kept next to the assertions that use it. The
#: authority is `pyproject.toml`; `test_packaging.py` pins that string, and
#: these tests check the environment actually satisfies it.
MIN_PYDANTIC_AI = "2.37.0"
MAX_PYDANTIC_AI_EXCLUSIVE = "2.38"
SUPPORTED_PYTHON = ((3, 10), (3, 11), (3, 12), (3, 13))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def crm_fetch(account: str) -> str:
    return f"account {account}: ok"


def two_turn_model() -> FunctionModel:
    async def fn(messages: List[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not any(getattr(p, "part_kind", None) == "tool-return"
                   for m in messages for p in m.parts):
            return ModelResponse(
                parts=[ToolCallPart(tool_name="crm_fetch",
                                    args={"account": "A1"},
                                    tool_call_id="call-1")],
                usage=RequestUsage(input_tokens=10, output_tokens=5),
                model_name="m", provider_name="p")
        return ModelResponse(parts=[TextPart(content="done")],
                             usage=RequestUsage(input_tokens=30, output_tokens=7),
                             model_name="m", provider_name="p")
    return FunctionModel(fn)


def events(home: Path, session_id: str) -> List[dict]:
    path = home / ".sentience" / "traces" / "pydantic-ai" / f"{session_id}.jsonl"
    return ([json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            if path.exists() else [])


async def governed_trace(home: Path, *, objective: str = "Fix the timeout",
                         scope: Any = ("crm",)) -> List[dict]:
    capability = SentienceGovernor(objective=objective, scope=list(scope),
                                   agent_id="cp8-agent")
    result = await Agent(two_turn_model(),
                         tools=[Tool(crm_fetch, metadata=CRM_READ)],
                         capabilities=[capability]).run("go")
    return events(home, result.run_id)


# ---------------------------------------------------------------------------
# The running environment
# ---------------------------------------------------------------------------

def test_running_python_is_a_supported_version():
    """CI is meant to exercise 3.10 through 3.13.

    This fails on an interpreter outside the declared support window, so a
    matrix entry that quietly drifts off the list is caught by the job
    that runs on it rather than by reading the workflow file.
    """
    assert sys.version_info[:2] in SUPPORTED_PYTHON, (
        f"running on {sys.version_info[:2]}, which the distribution does "
        f"not declare support for")


def test_the_python_floor_is_the_lowest_supported_version():
    """The floor and the matrix must not drift apart."""
    assert min(SUPPORTED_PYTHON) == (3, 10)


def test_installed_pydantic_ai_is_inside_the_declared_range():
    """The bound is only real if the installed version honours it.

    CI installs both ends of the range, so this assertion runs against the
    floor and the ceiling in turn rather than against one convenient
    version on a developer's machine.
    """
    spec = SpecifierSet(f">={MIN_PYDANTIC_AI},<{MAX_PYDANTIC_AI_EXCLUSIVE}")
    found = Version(installed_version("pydantic-ai-slim"))
    assert found in spec, f"pydantic-ai-slim {found} is outside {spec}"


def test_the_minimum_supported_pydantic_ai_still_carries_the_hooks_we_use():
    """The floor is a claim about an API surface, so state which surface.

    Raising the floor without re-checking these is how a bound becomes
    decorative. Each name below is one the capability actually calls.
    """
    from pydantic_ai.capabilities import AbstractCapability

    for hook in ("for_run", "wrap_run", "wrap_tool_execute",
                 "after_model_request"):
        assert hasattr(AbstractCapability, hook), hook


def test_installed_governor_core_is_inside_the_declared_range():
    spec = SpecifierSet(">=0.3.1.2,<0.3.2")
    found = Version(installed_version("sentience-governor"))
    assert found in spec, f"sentience-governor {found} is outside {spec}"


# ---------------------------------------------------------------------------
# Analyzer compatibility — the trace has to be readable by shipped tools
# ---------------------------------------------------------------------------

async def test_compute_pulse_reads_a_pydantic_ai_session(isolated_home):
    """A real reading, not a smoke test.

    `compute_pulse` was written against Claude Code and MCP traces. If our
    events were shaped even slightly wrong it would still return a dict,
    which is why this asserts on the numbers inside it.
    """
    pulse = compute_pulse(await governed_trace(isolated_home))

    assert pulse["status"] == "ok"
    assert pulse["session_summary"]["total_turns"] == 2, (
        "both model turns were recognised as turns")
    assert pulse["undeclared_intent"]["session_has_declared_intent"] is True

    # POL-003 does not fire: the tool carried an explicit classification.
    assert pulse["advisory_flag_summary"]["CONTEXT_UNCLASSIFIED"] == 0
    # The call was in scope against the declared scope.
    assert pulse["advisory_flag_summary"]["SCOPE_INTENT_MISMATCH"] == 0


async def test_undeclared_intent_spend_sees_the_declared_session(
        isolated_home):
    """Intent was declared, so the analyzer must not read it as undeclared.

    This is the join that matters commercially: the capability declares
    intent at session open, and the shipped analyzer has to agree that it
    did. A session the analyzer reads as undeclared would put every token
    in the undeclared bucket and misprice the whole run.
    """
    spend = compute_undeclared_intent_spend(await governed_trace(isolated_home))

    assert spend["status"] == "ok"
    assert spend["session_has_declared_intent"] is True
    assert spend["undeclared_turn_count"] == 0
    assert spend["undeclared_turns"] == []
    assert spend["undeclared_tokens"] == 0
    assert spend["total_turn_count"] == 2

    # Nothing was skipped or repaired on the way in.
    assert spend["malformed_event_count"] == 0
    assert spend["unpaired_event_count"] == 0
    assert spend["dedupe_conflict_count"] == 0
    assert spend["warnings"] == []


async def test_the_tool_call_is_attributed_to_the_right_operation(
        isolated_home):
    """CP4's classification and CP5's assertion survive into the analyzer."""
    spend = compute_undeclared_intent_spend(await governed_trace(isolated_home))

    assert spend["tool_calls"]["total"] == 1
    assert spend["tool_calls"]["by_operation"]["read"] == 1
    assert spend["tool_calls"]["by_operation"]["write"] == 0
    assert spend["tool_calls"]["by_tool"] == {"crm_fetch": 1}


async def test_token_totals_survive_the_analyzer(isolated_home):
    """CP6's per-turn attribution must add up the same way downstream.

    The two turns used 10/5 and 30/7. The analyzer has to reach 40 and 12
    on its own, which it can only do by reading both token snapshots and
    treating them as separate turns rather than deduplicating them.
    """
    spend = compute_undeclared_intent_spend(await governed_trace(isolated_home))

    assert spend["token_breakdown"]["prompt"] == 40
    assert spend["token_breakdown"]["completion"] == 12
    assert spend["total_tokens"] == 52
    # Nothing reported cache usage, and the analyzer does not invent any.
    assert spend["token_breakdown"]["cached_read"] == 0
    assert spend["token_breakdown"]["cached_write"] == 0


async def test_tool_bearing_turn_is_identified_by_the_analyzer(isolated_home):
    """The `tool_use_ids` join CP6 landed is what makes this attribution work."""
    spend = compute_undeclared_intent_spend(await governed_trace(isolated_home))

    attribution = spend["tool_token_attribution"]
    assert attribution["tokens_on_turns_with_tool_calls"] == 15, (
        "the first turn: 10 prompt + 5 completion")
    assert attribution["by_tool"] == [
        {"tool_id": "crm_fetch", "tokens": 15, "turn_count": 1}]


async def test_analyzers_do_not_raise_on_an_empty_trace():
    """Both are documented as raising nothing; hold them to it.

    A capability that opens a session and emits nothing is a real state,
    not a hypothetical: it is what a run that fails before its first hook
    leaves behind.
    """
    assert compute_pulse([])["status"]
    assert compute_undeclared_intent_spend([])["status"]
