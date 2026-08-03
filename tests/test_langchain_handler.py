"""Tests for SentienceCallbackHandler intent classification.

Covers the intent-declaration-honesty fix: strings
extracted from the LangChain chain's invocation inputs must be
classified as IntentSource.inferred (not IntentSource.explicit),
with IntentConfidence.inferred_low (not inferred_high).

The distinction matters for the audit trail: IntentSource.explicit
is reserved for strings the integrator declared at wrapper
construction time (via wrap_mcp_client). Strings extracted from
runtime invocation inputs (e.g. inputs["input"] in a LangChain
agent invocation) are from a different trust source — usually a
user request — and must not be conflated with integrator-declared
intent.

These tests also serve as a regression guard against any future
change that accidentally reverts to labelling extracted strings
as explicit.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.schema.events import (
    GovernanceEvent,
    IntentConfidence,
    IntentSource,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import SinkWriter
from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------


class _CapturingSink:
    """Minimal sink that collects every emitted event."""

    def __init__(self) -> None:
        self.events: List[GovernanceEvent] = []

    def write(self, event: GovernanceEvent) -> bool:
        self.events.append(event)
        return True


def _make_handler() -> tuple[_CapturingSink, SentienceCallbackHandler]:
    """Construct a SentienceCallbackHandler wired to a capturing sink."""
    captured = _CapturingSink()
    sm = SessionManager()
    cache = InProcessCache()
    sink = SinkWriter(captured)
    handler = SentienceCallbackHandler(
        agent_id="test-langchain-agent",
        session_manager=sm,
        cache=cache,
        sink_writer=sink,
        agent_version="1.0.0",
        vendor_id="test-vendor",
        declared_capabilities=["test.read"],
        owner_claim="user-test",
    )
    return captured, handler


def _intent_event(events: List[GovernanceEvent]) -> GovernanceEvent:
    """Return the single INTENT_DECLARED event from a captured list."""
    intent_events = [
        e for e in events if e.event_type.value == "INTENT_DECLARED"
    ]
    assert len(intent_events) == 1, (
        f"expected exactly one INTENT_DECLARED event, got {len(intent_events)}"
    )
    return intent_events[0]


# ---------------------------------------------------------------------------
# D5.1 — input-extracted intent must classify as inferred + inferred_low
# ---------------------------------------------------------------------------


class TestLangChainInputExtractedIntent:
    """When the handler extracts a string from chain inputs, the resulting
    INTENT_DECLARED event must carry intent_source=inferred and
    intent_confidence=inferred_low — never explicit.

    The string came from invocation context (typically a user request),
    not from an integrator-supplied stated_objective. This distinction
    is load-bearing for the audit trail.
    """

    def test_input_key_produces_inferred_intent(self) -> None:
        """The 'input' key is the most common LangChain invocation
        input. Strings extracted from it must classify as inferred."""
        captured, handler = _make_handler()
        handler.on_chain_start(
            serialized={},
            inputs={"input": "Look up customer Acme Corp"},
        )

        intent = _intent_event(captured.events)
        assert intent.payload.intent_source == IntentSource.inferred
        assert intent.payload.intent_confidence == IntentConfidence.inferred_low
        assert intent.payload.stated_objective == "Look up customer Acme Corp"

    def test_question_key_produces_inferred_intent(self) -> None:
        """Other recognised input keys (question, objective, task,
        prompt) must also classify as inferred."""
        captured, handler = _make_handler()
        handler.on_chain_start(
            serialized={},
            inputs={"question": "What's the status of order 12345?"},
        )

        intent = _intent_event(captured.events)
        assert intent.payload.intent_source == IntentSource.inferred
        assert intent.payload.intent_confidence == IntentConfidence.inferred_low
        assert intent.payload.stated_objective == "What's the status of order 12345?"


# ---------------------------------------------------------------------------
# D5.2 — no extractable input -> IntentSource.none
# ---------------------------------------------------------------------------


class TestLangChainNoInputIntent:
    """When no extractable string is present in the chain inputs, the
    emitted INTENT_DECLARED event must carry intent_source=none and
    intent_confidence=unknown, unchanged from pre-fix behaviour."""

    def test_missing_input_keys_produces_none(self) -> None:
        captured, handler = _make_handler()
        handler.on_chain_start(
            serialized={},
            inputs={"unrelated_key": "some value"},
        )

        intent = _intent_event(captured.events)
        assert intent.payload.intent_source == IntentSource.none
        assert intent.payload.intent_confidence == IntentConfidence.unknown
        assert intent.payload.stated_objective is None

    def test_empty_inputs_produces_none(self) -> None:
        captured, handler = _make_handler()
        handler.on_chain_start(serialized={}, inputs={})

        intent = _intent_event(captured.events)
        assert intent.payload.intent_source == IntentSource.none


# ---------------------------------------------------------------------------
# D5.3 — regression guard: input-extracted strings are never explicit
# ---------------------------------------------------------------------------


class TestLangChainExplicitRegressionGuard:
    """Regression guard for the fix documented in
    the intent-declaration-honesty fix.

    Before the fix, the LangChain handler classified any extracted
    string as IntentSource.explicit — the same classification used
    for integrator-supplied stated_objective in the MCP path. That
    conflated two epistemically different sources and weakened the
    audit trail.

    This test asserts the fix does not regress: input-extracted
    strings MUST NOT carry IntentSource.explicit or
    IntentConfidence.explicit, regardless of which recognised input
    key the string came from.
    """

    @pytest.mark.parametrize(
        "input_key",
        ["input", "question", "objective", "task", "prompt"],
    )
    def test_input_extracted_never_classified_as_explicit(
        self, input_key: str
    ) -> None:
        captured, handler = _make_handler()
        handler.on_chain_start(
            serialized={},
            inputs={input_key: "some extracted string"},
        )

        intent = _intent_event(captured.events)
        assert intent.payload.intent_source != IntentSource.explicit, (
            f"input key {input_key!r} produced explicit intent_source; "
            "the LangChain extraction path must never produce this value"
        )
        assert intent.payload.intent_confidence != IntentConfidence.explicit, (
            f"input key {input_key!r} produced explicit intent_confidence; "
            "the LangChain extraction path must never produce this value"
        )


# ---------------------------------------------------------------------------
# v0.2.3 Track 2 — token tracking via on_llm_start / on_llm_end
# ---------------------------------------------------------------------------


class _FakeLLMResponse:
    """Stand-in for a LangChain LLM response object."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


def _ctx_events(events: List[GovernanceEvent]) -> List[GovernanceEvent]:
    return [e for e in events if e.event_type.value == "CONTEXT_SNAPSHOT"]


class TestOnLLMStartReset:
    """on_llm_start is the turn-boundary reset point."""

    def test_method_exists_and_callable(self) -> None:
        _, handler = _make_handler()
        # Must not raise.
        handler.on_llm_start(serialized={}, prompts=["hello"])

    def test_resets_pending_usage(self) -> None:
        _, handler = _make_handler()
        # Pre-populate as if a prior turn ran.
        handler._pending_llm_usage = {
            "llm_prompt_tokens": 100,
            "llm_completion_tokens": 50,
            "llm_cached_read_tokens": None,
            "llm_cached_write_tokens": None,
            "llm_reasoning_tokens": None,
        }
        handler.on_llm_start(serialized={}, prompts=[])
        for value in handler._pending_llm_usage.values():
            assert value is None

    def test_allocates_new_turn_id(self) -> None:
        _, handler = _make_handler()
        handler.on_llm_start(serialized={}, prompts=[])
        assert handler._pending_llm_turn_id is not None
        assert isinstance(handler._pending_llm_turn_id, str)
        assert len(handler._pending_llm_turn_id) > 0

    def test_consecutive_starts_produce_different_turn_ids(self) -> None:
        _, handler = _make_handler()
        handler.on_llm_start(serialized={}, prompts=[])
        first = handler._pending_llm_turn_id
        handler.on_llm_start(serialized={}, prompts=[])
        second = handler._pending_llm_turn_id
        assert first != second


class TestOnLLMEnd:
    """on_llm_end captures usage; does NOT regenerate turn id."""

    def test_method_exists_and_callable(self) -> None:
        _, handler = _make_handler()
        handler.on_llm_end(_FakeLLMResponse())

    def test_extracts_usage_from_usage_metadata(self) -> None:
        _, handler = _make_handler()
        handler.on_llm_start(serialized={}, prompts=[])
        response = _FakeLLMResponse(
            usage_metadata={"input_tokens": 100, "output_tokens": 50}
        )
        handler.on_llm_end(response)
        assert handler._pending_llm_usage["llm_prompt_tokens"] == 100
        assert handler._pending_llm_usage["llm_completion_tokens"] == 50

    def test_does_not_regenerate_turn_id(self) -> None:
        """Same turn id must persist from on_llm_start through tool calls."""
        _, handler = _make_handler()
        handler.on_llm_start(serialized={}, prompts=[])
        original_id = handler._pending_llm_turn_id
        handler.on_llm_end(_FakeLLMResponse(usage_metadata={"input_tokens": 100}))
        assert handler._pending_llm_turn_id == original_id

    def test_extraction_exception_does_not_propagate(self) -> None:
        """Defensive: a bad response object must not crash the handler."""
        _, handler = _make_handler()
        handler.on_llm_start(serialized={}, prompts=[])
        # Object that explodes on attribute access.
        class _Boom:
            def __getattr__(self, name: str) -> Any:
                raise RuntimeError("simulated failure")
        # Must not raise; usage falls back to all-None.
        handler.on_llm_end(_Boom())
        for value in handler._pending_llm_usage.values():
            assert value is None


class TestTokenAttributionInToolCallEvents:
    """Verify the full lifecycle: start -> end -> tool_start -> emitted event."""

    def test_token_fields_attached_to_tool_call_events(self) -> None:
        captured, handler = _make_handler()
        handler.on_chain_start(serialized={}, inputs={"input": "hello"})
        handler.on_llm_start(serialized={}, prompts=[])
        handler.on_llm_end(
            _FakeLLMResponse(usage_metadata={"input_tokens": 100, "output_tokens": 50})
        )
        handler.on_tool_start(serialized={"name": "test_tool"}, input_str="x")

        ctx_events = _ctx_events(captured.events)
        assert len(ctx_events) >= 1
        last = ctx_events[-1]
        assert last.payload.llm_prompt_tokens == 100
        assert last.payload.llm_completion_tokens == 50
        assert last.payload.llm_turn_id is not None

    def test_one_turn_three_tool_calls_share_turn_id_and_usage(self) -> None:
        """Multi-tool-call attribution: same usage + turn id on every event."""
        captured, handler = _make_handler()
        handler.on_chain_start(serialized={}, inputs={"input": "hello"})
        handler.on_llm_start(serialized={}, prompts=[])
        handler.on_llm_end(
            _FakeLLMResponse(usage_metadata={"input_tokens": 100, "output_tokens": 50})
        )
        for i in range(3):
            handler.on_tool_start(
                serialized={"name": f"tool_{i}"}, input_str=f"call_{i}"
            )

        ctx_events = _ctx_events(captured.events)
        # 3 tool starts each emit 1 ctx snapshot.
        tool_call_events = [e for e in ctx_events if "tool_" in e.payload.provenance[0]]
        assert len(tool_call_events) == 3

        turn_ids = {e.payload.llm_turn_id for e in tool_call_events}
        assert len(turn_ids) == 1, (
            f"all 3 tool-call events must share one turn id, got {turn_ids}"
        )
        prompt_tokens = {e.payload.llm_prompt_tokens for e in tool_call_events}
        assert prompt_tokens == {100}

    def test_turn_boundary_changes_turn_id(self) -> None:
        """Events from turn N+1 must carry a different turn id than turn N."""
        captured, handler = _make_handler()
        handler.on_chain_start(serialized={}, inputs={"input": "hello"})

        # Turn 1
        handler.on_llm_start(serialized={}, prompts=[])
        handler.on_llm_end(
            _FakeLLMResponse(usage_metadata={"input_tokens": 100, "output_tokens": 50})
        )
        handler.on_tool_start(serialized={"name": "tool_a"}, input_str="x")

        # Turn 2 (boundary reset)
        handler.on_llm_start(serialized={}, prompts=[])
        handler.on_llm_end(
            _FakeLLMResponse(usage_metadata={"input_tokens": 200, "output_tokens": 75})
        )
        handler.on_tool_start(serialized={"name": "tool_b"}, input_str="y")

        ctx_events = _ctx_events(captured.events)
        tool_a_events = [
            e for e in ctx_events if e.payload.provenance == ["tool_a"]
        ]
        tool_b_events = [
            e for e in ctx_events if e.payload.provenance == ["tool_b"]
        ]
        assert tool_a_events and tool_b_events
        assert (
            tool_a_events[0].payload.llm_turn_id
            != tool_b_events[0].payload.llm_turn_id
        ), "turn id from turn N must not leak into turn N+1"
        # Token counts also differ across turns.
        assert tool_a_events[0].payload.llm_prompt_tokens == 100
        assert tool_b_events[0].payload.llm_prompt_tokens == 200


class TestOutOfOrderImmutability:
    """Trace immutability rule (§3.2): emitted events are never mutated.

    If on_tool_start fires before on_llm_end, the emitted event carries
    the turn id (if on_llm_start ran) but no token fields. When
    on_llm_end later arrives with usage, it does NOT mutate the prior
    event — that event stays as it was emitted.
    """

    def test_tool_before_llm_end_emits_no_token_fields(self) -> None:
        captured, handler = _make_handler()
        handler.on_chain_start(serialized={}, inputs={"input": "hello"})
        handler.on_llm_start(serialized={}, prompts=[])
        # NOTE: on_llm_end has NOT run yet.
        handler.on_tool_start(serialized={"name": "early_tool"}, input_str="x")

        ctx_events = _ctx_events(captured.events)
        early = [e for e in ctx_events if e.payload.provenance == ["early_tool"]][-1]
        # Turn id present (allocated by on_llm_start).
        assert early.payload.llm_turn_id is not None
        # Token fields absent — on_llm_end never ran for this turn yet.
        assert early.payload.llm_prompt_tokens is None
        assert early.payload.llm_completion_tokens is None

    def test_late_arriving_usage_does_not_mutate_prior_event(self) -> None:
        captured, handler = _make_handler()
        handler.on_chain_start(serialized={}, inputs={"input": "hello"})
        handler.on_llm_start(serialized={}, prompts=[])
        handler.on_tool_start(serialized={"name": "early_tool"}, input_str="x")

        # Capture the event reference BEFORE on_llm_end.
        ctx_events = _ctx_events(captured.events)
        early = [e for e in ctx_events if e.payload.provenance == ["early_tool"]][-1]
        # Snapshot the values that should never change.
        snapshotted_prompt = early.payload.llm_prompt_tokens
        snapshotted_completion = early.payload.llm_completion_tokens

        # NOW on_llm_end arrives with token data.
        handler.on_llm_end(
            _FakeLLMResponse(usage_metadata={"input_tokens": 100, "output_tokens": 50})
        )

        # The earlier event must NOT be mutated retroactively.
        assert early.payload.llm_prompt_tokens is snapshotted_prompt
        assert early.payload.llm_completion_tokens is snapshotted_completion


# ---------------------------------------------------------------------------
# v0.2.3 Track 2 — SentienceMiddleware awrap_step + lifecycle
# ---------------------------------------------------------------------------

import asyncio
import pytest

from sentience_governor.wrapper.langchain_adapter import SentienceMiddleware


class _FakeAIMessage:
    """Stand-in for a LangChain AIMessage with usage_metadata."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeState:
    def __init__(self, messages: List[Any]) -> None:
        self.messages = messages


class TestMiddlewareAwrapStep:
    """awrap_step: aggregation per §3.3.1, lifecycle per §3.3.2."""

    def test_method_exists(self) -> None:
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        assert hasattr(mw, "awrap_step")
        assert callable(mw.awrap_step)

    def test_aggregates_single_message(self) -> None:
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        msg = _FakeAIMessage(
            usage_metadata={"input_tokens": 100, "output_tokens": 50}
        )
        state_in = _FakeState(messages=[])
        state_out = _FakeState(messages=[msg])

        async def next_call(state: Any) -> str:
            # Inside the step: state must be set.
            assert mw._current_step_usage is not None
            assert mw._current_step_turn_id is not None
            return "ok"

        result = asyncio.run(mw.awrap_step(state_in, state_out, next_call))
        assert result == "ok"

    def test_state_cleared_after_step(self) -> None:
        """Lifecycle: state must be cleared in finally to prevent
        cross-step leakage."""
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        msg = _FakeAIMessage(
            usage_metadata={"input_tokens": 100, "output_tokens": 50}
        )
        state_in = _FakeState(messages=[])
        state_out = _FakeState(messages=[msg])

        async def next_call(state: Any) -> str:
            return "ok"

        asyncio.run(mw.awrap_step(state_in, state_out, next_call))

        # After step completion, all state must be None.
        assert mw._current_step_usage is None
        assert mw._current_step_model is None
        assert mw._current_step_provider is None
        assert mw._current_step_turn_id is None

    def test_state_cleared_even_on_exception(self) -> None:
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        msg = _FakeAIMessage(
            usage_metadata={"input_tokens": 100, "output_tokens": 50}
        )
        state_in = _FakeState(messages=[])
        state_out = _FakeState(messages=[msg])

        async def next_call(state: Any) -> str:
            raise RuntimeError("simulated")

        with pytest.raises(RuntimeError):
            asyncio.run(mw.awrap_step(state_in, state_out, next_call))

        # State still cleared by finally block.
        assert mw._current_step_turn_id is None

    def test_aggregate_sums_across_messages(self) -> None:
        """Per §3.3.1: numeric fields sum across messages."""
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        msg1 = _FakeAIMessage(usage_metadata={"input_tokens": 100, "output_tokens": 50})
        msg2 = _FakeAIMessage(usage_metadata={"input_tokens": 30, "output_tokens": 20})
        usage, _, _ = mw._aggregate_messages([msg1, msg2])
        assert usage["llm_prompt_tokens"] == 130
        assert usage["llm_completion_tokens"] == 70

    def test_aggregate_skips_none_contributions(self) -> None:
        """Per §3.3.1: None contributions are skipped, NOT treated as 0."""
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        msg1 = _FakeAIMessage(usage_metadata={"input_tokens": 100, "output_tokens": 50})
        msg2 = _FakeAIMessage()  # no usage_metadata; contributes None
        usage, _, _ = mw._aggregate_messages([msg1, msg2])
        # 100 + None should be 100, not 100 + 0.
        assert usage["llm_prompt_tokens"] == 100
        assert usage["llm_completion_tokens"] == 50

    def test_aggregate_all_none_yields_none_not_zero(self) -> None:
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        msg1 = _FakeAIMessage()
        msg2 = _FakeAIMessage()
        usage, model, provider = mw._aggregate_messages([msg1, msg2])
        for value in usage.values():
            assert value is None
        assert model is None
        assert provider is None

    def test_aggregate_consistent_model_preserved(self) -> None:
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        msg1 = _FakeAIMessage(
            response_metadata={"model_name": "claude-sonnet-4-5"}
        )
        msg2 = _FakeAIMessage(
            response_metadata={"model_name": "claude-sonnet-4-5"}
        )
        _, model, _ = mw._aggregate_messages([msg1, msg2])
        assert model == "claude-sonnet-4-5"

    def test_aggregate_mixed_models_yields_none(self) -> None:
        """Per §3.3.1: mixed-model step → None (no magic 'multiple' sentinel)."""
        _, handler = _make_handler()
        mw = SentienceMiddleware(handler)
        msg1 = _FakeAIMessage(response_metadata={"model_name": "claude"})
        msg2 = _FakeAIMessage(response_metadata={"model_name": "gpt-4"})
        _, model, _ = mw._aggregate_messages([msg1, msg2])
        assert model is None


class TestMiddlewareBackwardCompat:
    """Existing awrap_tool_call must continue to work without awrap_step."""

    def test_awrap_tool_call_works_without_step_state(self) -> None:
        _, handler = _make_handler()
        handler.on_chain_start(serialized={}, inputs={"input": "hello"})
        mw = SentienceMiddleware(handler)

        async def next_call(_: Any) -> str:
            return "tool_result"

        # No awrap_step ever called; tool call must still work.
        result = asyncio.run(mw.awrap_tool_call("test_tool", "x", next_call))
        assert result == "tool_result"
