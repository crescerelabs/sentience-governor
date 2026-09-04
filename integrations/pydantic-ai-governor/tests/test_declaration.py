"""CP3 — declaration handling, and visible fail-open on malformed input.

The D1 obligations are tested as four properties, not as one: a malformed
declaration must be **not silent**, **not raising**, **visible to the
developer**, and **visible in the evidence**.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, List

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from pydantic_ai_governor import SentienceGovernor
from pydantic_ai_governor.declaration import Declaration, resolve, validate

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def answering_model() -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("done")])
    return FunctionModel(fn)


def trace_events(home: Path, session_id: str) -> List[dict]:
    path = home / ".sentience" / "traces" / "pydantic-ai" / f"{session_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def of_type(events: List[dict], event_type: str) -> List[dict]:
    return [e for e in events if e["event_type"] == event_type]


def governance_errors(capsys) -> List[dict]:
    """GOVERNANCE_ERROR events, read from stdout.

    Core routes them there deliberately and unconditionally:
    `SinkWriter.write` short-circuits on GOVERNANCE_ERROR and writes to
    stdout "regardless of configured sink" (`sink/writer.py:124-127`).
    The integration does not override that; reimplementing sink routing is
    exactly what it must not do.
    """
    out = capsys.readouterr().out
    events = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event_type") == "GOVERNANCE_ERROR":
            events.append(event)
    return events


def declared(events: List[dict]) -> dict:
    [event] = of_type(events, "INTENT_DECLARED")
    return event["payload"]


async def run(gov: SentienceGovernor, **kwargs: Any):
    return await Agent(answering_model(), capabilities=[gov]).run("go", **kwargs)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------

async def test_per_run_metadata_overrides_constructor_defaults(isolated_home):
    """One Agent, three runs: two declared per run, one bare."""
    gov = SentienceGovernor(objective="DEFAULT", scope=["crm"])
    agent = Agent(answering_model(), capabilities=[gov])

    r1 = await agent.run("a", metadata={"sentience_governor": {
        "objective": "RUN-ONE", "scope": ["crm"]}})
    r2 = await agent.run("b", metadata={"sentience_governor": {
        "objective": "RUN-TWO", "scope": ["billing"]}})
    r3 = await agent.run("c")

    p1 = declared(trace_events(isolated_home, r1.run_id))
    p2 = declared(trace_events(isolated_home, r2.run_id))
    p3 = declared(trace_events(isolated_home, r3.run_id))

    assert p1["stated_objective"] == "RUN-ONE"
    assert p2["stated_objective"] == "RUN-TWO"
    assert p1["session_scope_hint"] == ["crm"]
    assert p2["session_scope_hint"] == ["billing"]
    # No cross-run leakage: the bare run gets the constructor values, not
    # the previous run's.
    assert p3["stated_objective"] == "DEFAULT"
    assert p3["session_scope_hint"] == ["crm"]


async def test_declared_intent_is_explicit_on_both_axes(isolated_home):
    """Integrator-vouched at invocation time is stronger provenance than
    construction time, and the trace says so."""
    result = await run(SentienceGovernor(),
                       metadata={"sentience_governor": {
                           "objective": "Fix the timeout", "scope": ["crm"]}})
    payload = declared(trace_events(isolated_home, result.run_id))
    assert payload["intent_source"] == "explicit"
    assert payload["intent_confidence"] == "explicit"


async def test_undeclared_run_says_so_rather_than_inventing_one(isolated_home):
    """Nothing from either source. The event still fires, honestly."""
    result = await run(SentienceGovernor())
    payload = declared(trace_events(isolated_home, result.run_id))
    assert payload["stated_objective"] is None
    assert payload["intent_source"] == "none"
    assert payload["intent_confidence"] == "unknown"
    assert payload["session_scope_hint"] == []


async def test_registration_precedes_declaration(isolated_home):
    result = await run(SentienceGovernor(objective="o", scope=["crm"]))
    order = [e["event_type"] for e in trace_events(isolated_home, result.run_id)]
    assert order == ["AGENT_REGISTERED", "INTENT_DECLARED"]


async def test_partial_block_falls_back_key_by_key(isolated_home):
    """A valid block that omits a key inherits that key from the
    constructor. This is not the malformed path."""
    gov = SentienceGovernor(objective="DEFAULT", scope=["crm"])
    result = await run(gov, metadata={"sentience_governor": {
        "objective": "RUN-ONLY"}})
    payload = declared(trace_events(isolated_home, result.run_id))
    assert payload["stated_objective"] == "RUN-ONLY"
    assert payload["session_scope_hint"] == ["crm"]


# ---------------------------------------------------------------------------
# D1 — visible fail-open
# ---------------------------------------------------------------------------

MALFORMED = [
    pytest.param("not-a-mapping", id="block-is-a-string"),
    pytest.param({"objective": 42}, id="objective-not-a-string"),
    pytest.param({"objective": "  "}, id="objective-blank"),
    pytest.param({"scope": "crm"}, id="scope-is-a-string"),
    pytest.param({"scope": ["crm", 7]}, id="scope-item-not-a-string"),
    pytest.param({"objective": "ok", "scope": {"crm": True}}, id="scope-a-dict"),
    # Rev 5: unrecognised keys are malformed, not forward compatibility.
    pytest.param({"objectiv": "typo"}, id="misspelled-objective"),
    pytest.param({"objective": "ok", "scope": ["crm"], "extra": 1},
                 id="valid-fields-plus-unknown-key"),
]


@pytest.mark.parametrize("block", MALFORMED)
async def test_malformed_is_never_silent_and_never_raises(isolated_home, block, capsys):
    """Two of the four D1 obligations, over every malformed shape."""
    gov = SentienceGovernor(objective="DEFAULT", scope=["crm"])
    with pytest.warns(UserWarning, match="ignored this run's declaration"):
        await run(gov, metadata={"sentience_governor": block})

    errors = governance_errors(capsys)
    assert len(errors) == 1, "no GOVERNANCE_ERROR reached the evidence stream"
    assert errors[0]["payload"]["error_type"] == "SCHEMA_VIOLATION"
    assert errors[0]["payload"]["severity"] == "warning"
    assert errors[0]["payload"]["agent_continued"] is True


async def test_malformed_falls_back_to_constructor_defaults(isolated_home, capsys):
    """The run stays governed by whatever was validly supplied."""
    gov = SentienceGovernor(objective="DEFAULT", scope=["crm"])
    with pytest.warns(UserWarning):
        result = await run(gov, metadata={"sentience_governor": {"scope": "crm"}})

    payload = declared(trace_events(isolated_home, result.run_id))
    assert payload["stated_objective"] == "DEFAULT"
    assert payload["session_scope_hint"] == ["crm"]
    # And the reason says which declaration is in force, so the developer
    # is not left to guess.
    reason = governance_errors(capsys)[0]["payload"]["failure_reason"]
    assert "remain in force" in reason


async def test_malformed_with_no_constructor_default_stays_undeclared(
    isolated_home, capsys
):
    gov = SentienceGovernor()
    with pytest.warns(UserWarning):
        result = await run(gov, metadata={"sentience_governor": {"objective": 1}})

    payload = declared(trace_events(isolated_home, result.run_id))
    assert payload["stated_objective"] is None
    assert payload["intent_source"] == "none"
    reason = governance_errors(capsys)[0]["payload"]["failure_reason"]
    assert "undeclared" in reason


async def test_malformed_block_is_rejected_as_a_unit(isolated_home):
    """A good objective beside a broken scope must not yield a
    half-declaration presented as explicit."""
    gov = SentienceGovernor()
    with pytest.warns(UserWarning):
        result = await run(gov, metadata={"sentience_governor": {
            "objective": "THIS SHOULD NOT SURVIVE", "scope": 5}})

    payload = declared(trace_events(isolated_home, result.run_id))
    assert payload["stated_objective"] is None
    assert payload["intent_source"] == "none"


async def test_no_metadata_value_reaches_the_warning_or_the_trace(isolated_home, capsys):
    """`failure_reason` lands in a durable local trace, and a rejected
    declaration is exactly where a developer might have put something
    sensitive. The reason names the field and the contract, never content."""
    secret = "sk-live-DO-NOT-LEAK-8f3a"
    gov = SentienceGovernor()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # Malformed (scope must be a list), and carrying the secret in
        # BOTH fields, so a leak through either would show.
        result = await run(gov, metadata={"sentience_governor": {
            "objective": secret, "scope": secret}})

    assert caught, "no warning was raised"
    for warning in caught:
        assert secret not in str(warning.message)

    blob = json.dumps(trace_events(isolated_home, result.run_id))
    assert secret not in blob
    assert secret not in json.dumps(governance_errors(capsys))


async def test_execution_continues_and_output_is_unchanged(isolated_home):
    """Fail-open means the agent finishes normally."""
    gov = SentienceGovernor()
    with pytest.warns(UserWarning):
        governed = await run(gov, metadata={"sentience_governor": "broken"})
    plain = await Agent(answering_model()).run("go")
    assert governed.output == plain.output


async def test_missing_metadata_is_not_an_error(isolated_home, capsys):
    """Absent is not malformed. No warning, no GOVERNANCE_ERROR."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await run(SentienceGovernor(objective="o", scope=["crm"]))
    assert [w for w in caught if "declaration" in str(w.message)] == []
    assert governance_errors(capsys) == []


async def test_unrelated_metadata_namespace_is_ignored(isolated_home):
    """Another library's metadata is not our contract to police."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await run(SentienceGovernor(objective="o", scope=["crm"]),
                           metadata={"some_other_tool": {"whatever": 1}})
    assert [w for w in caught if "declaration" in str(w.message)] == []
    assert declared(trace_events(isolated_home, result.run_id))[
        "stated_objective"] == "o"


# ---------------------------------------------------------------------------
# The validator, directly
# ---------------------------------------------------------------------------

def test_validate_accepts_a_well_formed_block():
    assert validate({"objective": "o", "scope": ["crm"]}) is None
    assert validate({}) is None


def test_validate_rejects_unrecognised_keys():
    """Rev 5: an unrecognised key is malformed, not forward compatibility.
    An earlier implementation ignored them, which is withdrawn."""
    problem = validate({"objective": "o", "future_field": {"x": 1}})
    assert problem is not None
    assert "future_field" in problem


def test_resolve_leaves_defaults_untouched_when_no_block_is_present():
    default = Declaration(objective="o", scope=("crm",))
    resolved, rejection = resolve({}, default)
    assert resolved == default
    assert rejection is None


# ---------------------------------------------------------------------------
# Rev 5 — unrecognised keys are malformed
# ---------------------------------------------------------------------------

async def test_misspelled_key_is_rejected_rather_than_ignored(
    isolated_home, capsys
):
    """The case the decision exists for. A developer writing `objectiv`
    has declared nothing; silence would leave them believing otherwise."""
    gov = SentienceGovernor()
    with pytest.warns(UserWarning, match="ignored this run's declaration"):
        result = await run(gov, metadata={"sentience_governor": {
            "objectiv": "Fix the timeout"}})

    payload = declared(trace_events(isolated_home, result.run_id))
    assert payload["stated_objective"] is None
    assert payload["intent_source"] == "none"

    [error] = governance_errors(capsys)
    reason = error["payload"]["failure_reason"]
    assert "objectiv" in reason, "the unknown key must be named"
    assert "undeclared" in reason


async def test_unknown_key_rejects_the_whole_block_atomically(
    isolated_home, capsys
):
    """Valid fields beside an unknown one do NOT survive. Atomic rejection
    is what stops a half-declaration being presented as explicit."""
    gov = SentienceGovernor(objective="DEFAULT", scope=["billing"])
    with pytest.warns(UserWarning):
        result = await run(gov, metadata={"sentience_governor": {
            "objective": "THIS SHOULD NOT SURVIVE",
            "scope": ["crm"],
            "extra": True,
        }})

    payload = declared(trace_events(isolated_home, result.run_id))
    # Constructor defaults remain effective; nothing from the block lands.
    assert payload["stated_objective"] == "DEFAULT"
    assert payload["session_scope_hint"] == ["billing"]
    assert "remain in force" in governance_errors(capsys)[0]["payload"][
        "failure_reason"]


async def test_unknown_key_value_is_never_echoed(isolated_home, capsys):
    """The key is named so the typo is findable. Its value is not."""
    secret = "sk-live-UNKNOWN-KEY-VALUE-4b2c"
    gov = SentienceGovernor()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await run(gov, metadata={"sentience_governor": {
            "mystery_field": secret}})

    assert caught
    for warning in caught:
        assert "mystery_field" in str(warning.message)
        assert secret not in str(warning.message)

    blob = json.dumps(governance_errors(capsys))
    assert "mystery_field" in blob
    assert secret not in blob
    assert secret not in json.dumps(trace_events(isolated_home, result.run_id))


def test_multiple_unknown_keys_are_all_named():
    problem = validate({"alpha": 1, "beta": 2})
    assert "'alpha'" in problem and "'beta'" in problem


# ---------------------------------------------------------------------------
# Rev 5 — core owns GOVERNANCE_ERROR routing
# ---------------------------------------------------------------------------

async def test_governance_error_is_not_written_to_the_session_trace(
    isolated_home, capsys
):
    """Core short-circuits GOVERNANCE_ERROR to stdout regardless of the
    configured sink (`sink/writer.py:124-127`). The integration honors that
    rather than working around it, so the error is developer-visible and
    absent from the trace. Pinned so no later change quietly adds a
    trace-writing workaround."""
    gov = SentienceGovernor()
    with pytest.warns(UserWarning):
        result = await run(gov, metadata={"sentience_governor": "broken"})

    assert of_type(trace_events(isolated_home, result.run_id),
                   "GOVERNANCE_ERROR") == []
    assert len(governance_errors(capsys)) == 1
