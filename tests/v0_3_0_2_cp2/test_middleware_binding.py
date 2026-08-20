"""CP2 — obligations 17-20. Middleware binding under the new state model.

Middleware calls handler.on_tool_start/on_tool_end with NO callback ids
(langchain_adapter.py:503,506), so binding is by active-root count.
"""
import asyncio
import json

from conftest import as_dict, make_handler, pol001, sessions, rid
from sentience_governor.wrapper.langchain_adapter import SentienceMiddleware


async def _call(mw, tool_name="read_thing", value="x"):
    async def next_call(_):
        return value
    return await mw.awrap_tool_call(tool_name, {"q": value}, next_call)


def governance_errors_on_stdout(captured_out):
    """GOVERNANCE_ERROR never reaches the configured sink.

    ``SinkWriter.write`` routes it to stdout regardless of which sink is
    configured (sink/writer.py), so this is where it must be asserted.
    """
    found = []
    for line in captured_out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("event_type") == "GOVERNANCE_ERROR":
            found.append(record)
    return found


def payloads(events):
    return [as_dict(e).get("payload") or {} for e in events]


# ---------------------------------------------------------------- 17
def test_17_middleware_binds_to_the_single_active_root():
    """REGRESSION: with exactly one root open, middleware events attach to it."""
    sink, h = make_handler(declared=["read_thing"])
    mw = SentienceMiddleware(h)
    root = rid()
    h.on_chain_start({"name": "root"}, {}, run_id=root, parent_run_id=None)
    asyncio.run(_call(mw))

    sids = {s for s in sessions(sink.events) if s}
    assert len(sids) == 1, "middleware tool events must join the one active root"
    assert pol001(sink.events) == [], "in-scope middleware tool must not fire POL-001"


# ---------------------------------------------------------------- 18
def test_18_middleware_with_no_active_root_uses_legacy_path():
    """REGRESSION: zero active roots -> legacy behaviour, still emits."""
    sink, h = make_handler(declared=["read_thing"])
    mw = SentienceMiddleware(h)
    result = asyncio.run(_call(mw, value="no-root"))
    assert result == "no-root", "the tool call itself must succeed (fail-open)"
    assert sessions(sink.events) == [] or all(s is None for s in sessions(sink.events)), \
        "with no active root, middleware must not invent a session"


# ---------------------------------------------------------------- 19
def test_19_middleware_with_two_active_roots_refuses_to_guess(capsys):
    """CONTRACT: ambiguous binding must emit GOVERNANCE_ERROR and no governance
    event, never silently land in a root or the legacy slot."""
    sink, h = make_handler(declared=["read_thing"])
    mw = SentienceMiddleware(h)
    a, b = rid(), rid()
    h.on_chain_start({"name": "A"}, {}, run_id=a, parent_run_id=None)
    h.on_chain_start({"name": "B"}, {}, run_id=b, parent_run_id=None)
    before = len(sink.events)
    capsys.readouterr()                       # drop anything emitted during setup
    asyncio.run(_call(mw))
    captured = capsys.readouterr().out
    new = sink.events[before:]

    scope_events = [e for e in new
                    if "SCOPE_ASSERTED" in str(as_dict(e).get("event_type", ""))]
    assert scope_events == [], \
        "ambiguous middleware binding must not emit a governance event"
    errors = governance_errors_on_stdout(captured)
    assert errors, \
        "ambiguous middleware binding must emit GOVERNANCE_ERROR on stdout"
    assert not any(as_dict(e).get("session_id") for e in new), \
        "ambiguous binding must not attribute the tool call to any session"


# ---------------------------------------------------------------- 20
def test_20_middleware_step_turn_id_does_not_outlive_its_step():
    """REGRESSION: snapshot/restore must stop a step turn id leaking onward."""
    sink, h = make_handler(declared=["read_thing"])
    mw = SentienceMiddleware(h)
    root = rid()
    h.on_chain_start({"name": "root"}, {}, run_id=root, parent_run_id=None)

    mw._current_step_turn_id = "STEP-TURN"
    mw._current_step_usage = {"llm_completion_tokens": 42}
    mw._current_step_model = "step-model"
    mw._current_step_provider = "step-provider"
    in_step_from = len(sink.events)
    asyncio.run(_call(mw, value="in-step"))

    # Positive control: without this the leak assertion below could pass
    # simply because the step turn id never reached an event at all.
    # Turn telemetry lives in the event payload, not at the top level.
    in_step = payloads(sink.events[in_step_from:])
    assert any(p.get("llm_turn_id") == "STEP-TURN" for p in in_step), \
        "the step turn id must reach events emitted DURING the step"

    mw._current_step_turn_id = None
    mw._current_step_usage = None
    mw._current_step_model = None
    mw._current_step_provider = None
    before = len(sink.events)
    asyncio.run(_call(mw, value="after-step"))

    after = payloads(sink.events[before:])
    assert after, "the post-step tool call must still be governed"
    leaked = [p for p in after if p.get("llm_turn_id") == "STEP-TURN"]
    assert leaked == [], "a step turn id must not outlive its step"
