"""Wrapper acceptance: MCPClientLike abstraction, wrap_mcp_client, classification hook.

Covers
------
* MCPClientLike protocol contract and SentienceMCPAdapter translation
* wrap_mcp_client + async context manager lifecycle
* classification_hook contract:
    - no hook configured       -> defaults
    - full hint                -> every field honoured
    - partial hint             -> merge with defaults (None-vs-empty semantic)
    - hook returns None        -> defaults, no warning
    - hook raises              -> defaults + bounded warning log
    - tool raises              -> only SCOPE_ASSERTED, no hook call, exception propagates
    - memory write injection   -> write_classification + retention_requested honoured
    - None vs intentional []   -> load-bearing distinction preserved
* Wrapper-level structural acceptance (TestWrapperAcceptance):
    - single persistence write call produces a clean 5-event session trace
    - multi-call read+write session produces a clean 7-event session trace
    - ordering / sequence / chain integrity through the public API
    - explicitly NOT a byte-for-byte golden trace match (that lives in
      tests/test_golden_trace_acceptance.py at the EventBuilder layer)
"""

from __future__ import annotations

import json
import logging
from typing import Any, List, Optional

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.schema.events import (
    ClassificationSource,
    DeploymentMode,
    GovernanceEvent,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import SinkWriter, StdoutSink
from sentience_governor.wrapper.mcp import (
    ClassificationHint,
    ClassificationHook,
    MCPClientLike,
    SentienceMCPAdapter,
    wrap_mcp_client,
)


# ---------------------------------------------------------------------------
# Original abstraction tests (unchanged — kept as-is)
# ---------------------------------------------------------------------------


class MockConcreteClient:
    """Simulates a concrete SDK client with a proprietary method name."""
    def __init__(self):
        self.calls = []

    def invoke(self, name: str, args: dict) -> dict:
        self.calls.append((name, args))
        return {"result": f"ok:{name}"}


class TestMCPClientLikeAbstraction:
    def test_protocol_contract(self):
        """MCPClientLike defines send_tool_call(tool_name, arguments) -> dict."""
        client = MCPClientLike()
        with pytest.raises(NotImplementedError):
            client.send_tool_call("tool", {})

    def test_adapter_translates_sdk(self):
        """SentienceMCPAdapter bridges concrete SDK to MCPClientLike."""
        mock = MockConcreteClient()
        adapter = SentienceMCPAdapter(
            delegate=mock,
            call_fn=lambda client, name, args: client.invoke(name, args),
        )
        result = adapter.send_tool_call("crm.fetch", {"id": "1"})
        assert result == {"result": "ok:crm.fetch"}
        assert mock.calls == [("crm.fetch", {"id": "1"})]

    def test_no_concrete_sdk_assumed(self):
        """wrap_mcp_client() targets MCPClientLike — no SDK method assumed."""
        mock = MockConcreteClient()
        adapter = SentienceMCPAdapter(
            delegate=mock,
            call_fn=lambda c, n, a: c.invoke(n, a),
        )
        sm = SessionManager()
        cache = InProcessCache()
        sink = SinkWriter(StdoutSink())

        wrapped = wrap_mcp_client(
            target=adapter,
            session_manager=sm,
            cache=cache,
            sink_writer=sink,
            agent_id="test-agent",
            stated_objective="Test objective",
            declared_capabilities=["crm.read"],
        )
        # Verify wrapped is a proxy (not the concrete client)
        assert wrapped is not adapter
        assert wrapped is not mock

    @pytest.mark.asyncio
    async def test_async_context_manager(self, capsys):
        """Wrapped session works as async context manager."""
        mock = MockConcreteClient()
        adapter = SentienceMCPAdapter(
            delegate=mock,
            call_fn=lambda c, n, a: c.invoke(n, a),
        )
        sm = SessionManager()
        cache = InProcessCache()
        sink = SinkWriter(StdoutSink())

        wrapped = wrap_mcp_client(
            target=adapter,
            session_manager=sm,
            cache=cache,
            sink_writer=sink,
            agent_id="test-agent",
            stated_objective="Test objective",
            declared_capabilities=["crm.read"],
            session_id="sess-test-001",
        )

        async with wrapped:
            result = wrapped.send_tool_call("crm.fetch", {"id": "1"})
            assert result == {"result": "ok:crm.fetch"}

        # Events were emitted to stdout
        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().split("\n") if l]
        assert len(lines) >= 2  # at least AGENT_REGISTERED + INTENT_DECLARED


# ---------------------------------------------------------------------------
# Classification hook tests — the D5 test list
# ---------------------------------------------------------------------------


class _CapturingSink:
    """Collect every GovernanceEvent emitted by the wrapper.

    Lets tests assert on event count, event_type ordering, and payload
    field values without touching stdout or the filesystem.
    """

    def __init__(self) -> None:
        self.events: List[GovernanceEvent] = []

    def write(self, event: GovernanceEvent) -> bool:
        self.events.append(event)
        return True


def _make_test_client_and_hook_context(
    *,
    tool_result: Any = None,
    tool_raises: Optional[Exception] = None,
    classification_hook: Optional[ClassificationHook] = None,
    is_persistence: bool = False,
) -> tuple[_CapturingSink, Any]:
    """Return (sink, wrapped_client) wired for a hook-focused test.

    The underlying ``FakeMCPClient`` returns ``tool_result`` (default:
    ``{"data": "ok"}``) or raises ``tool_raises`` if set. If ``is_persistence``
    is True, the tool name is ``vector_store.upsert`` so the wrapper's
    persistence-target heuristic detects it; otherwise it's ``crm.fetch``.
    """
    if tool_result is None:
        tool_result = {"data": "ok"}

    captured = _CapturingSink()

    class FakeMCPClient(MCPClientLike):
        def send_tool_call(self, tool_name: str, arguments: dict) -> Any:
            if tool_raises is not None:
                raise tool_raises
            return tool_result

    sm = SessionManager()
    cache = InProcessCache()
    sink = SinkWriter(captured)

    wrapped = wrap_mcp_client(
        target=FakeMCPClient(),
        session_manager=sm,
        cache=cache,
        sink_writer=sink,
        agent_id="hook-test-agent",
        stated_objective="Exercise the hook",
        declared_capabilities=["test.read", "test.write"],
        session_id="sess-hook-test",
        classification_hook=classification_hook,
    )
    return captured, wrapped


def _events_by_type(events: List[GovernanceEvent], event_type: str) -> List[GovernanceEvent]:
    return [e for e in events if e.event_type.value == event_type]


class TestClassificationHookContract:
    """Full coverage of the D5 contract tests for classification_hook."""

    # ------------------------------------------------------------------
    # D5.1: no hook configured -> defaults used
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_no_hook_configured_uses_defaults(self):
        captured, wrapped = _make_test_client_and_hook_context(
            tool_result={"response_field": "response_value"},
            classification_hook=None,
        )
        async with wrapped:
            wrapped.send_tool_call("crm.fetch", {"id": "1"})

        ctx_events = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")
        assert len(ctx_events) == 1
        ctx = ctx_events[0]
        payload = ctx.payload
        assert payload.data_classifications == []
        assert payload.classification_source == ClassificationSource.unclassified
        assert payload.provenance == ["crm"]
        assert payload.retention_flags == []
        # With no hook, context_size_tokens defaults to the response estimate.
        # The fake result was {"response_field": "response_value"} which is
        # ~32 bytes → ~8 tokens via the wrapper's rough estimator. Just
        # verify it is a positive integer rather than pinning a specific value.
        assert isinstance(payload.context_size_tokens, int)
        assert payload.context_size_tokens > 0

    # ------------------------------------------------------------------
    # D5.2: full hint -> every field honoured end to end
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_full_hint_every_field_honoured(self):
        def hook(tool_name: str, args: dict, result: Any) -> ClassificationHint:
            return ClassificationHint(
                data_classifications=["internal", "pii"],
                classification_source=ClassificationSource.vendor,
                provenance=["crm", "user_input"],
                retention_flags=["may-persist", "audit-trail"],
                context_size_tokens=1200,
            )

        captured, wrapped = _make_test_client_and_hook_context(
            classification_hook=hook,
        )
        async with wrapped:
            wrapped.send_tool_call("crm.fetch", {"id": "1"})

        ctx = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")[0]
        payload = ctx.payload
        assert payload.data_classifications == ["internal", "pii"]
        assert payload.classification_source == ClassificationSource.vendor
        assert payload.provenance == ["crm", "user_input"]
        assert payload.retention_flags == ["may-persist", "audit-trail"]
        assert payload.context_size_tokens == 1200

    # ------------------------------------------------------------------
    # D5.3: partial hint -> merge logic applied correctly
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_partial_hint_merges_with_defaults(self):
        """Hook populates only classifications + source; other fields fall
        back to wrapper defaults."""

        def hook(tool_name: str, args: dict, result: Any) -> ClassificationHint:
            return ClassificationHint(
                data_classifications=["internal"],
                classification_source=ClassificationSource.vendor,
                # provenance, retention_flags, context_size_tokens left as None
            )

        captured, wrapped = _make_test_client_and_hook_context(
            classification_hook=hook,
        )
        async with wrapped:
            wrapped.send_tool_call("crm.fetch", {"id": "1"})

        ctx = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")[0]
        payload = ctx.payload
        # Hint-supplied fields honoured
        assert payload.data_classifications == ["internal"]
        assert payload.classification_source == ClassificationSource.vendor
        # Unpopulated fields fall back to wrapper defaults
        assert payload.provenance == ["crm"]  # default = [target_system]
        assert payload.retention_flags == []  # default = []
        assert isinstance(payload.context_size_tokens, int)

    # ------------------------------------------------------------------
    # D5.4: hook returns None -> defaults used silently
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_hook_returns_none_uses_defaults_silently(self, caplog):
        def hook(tool_name: str, args: dict, result: Any) -> None:
            return None

        captured, wrapped = _make_test_client_and_hook_context(
            classification_hook=hook,
        )
        with caplog.at_level(logging.WARNING, logger="sentience_governor.wrapper.mcp"):
            async with wrapped:
                wrapped.send_tool_call("crm.fetch", {"id": "1"})

        ctx = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")[0]
        assert ctx.payload.data_classifications == []
        assert ctx.payload.classification_source == ClassificationSource.unclassified
        # Returning None must NOT trigger a warning log
        assert not any("classification_hook raised" in rec.message for rec in caplog.records)

    # ------------------------------------------------------------------
    # D5.5: hook raises -> defaults + bounded warning log
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_hook_raises_uses_defaults_with_bounded_warning(self, caplog):
        def hook(tool_name: str, args: dict, result: Any) -> ClassificationHint:
            raise ValueError("synthetic hook failure")

        captured, wrapped = _make_test_client_and_hook_context(
            classification_hook=hook,
        )
        with caplog.at_level(logging.WARNING, logger="sentience_governor.wrapper.mcp"):
            async with wrapped:
                # Agent must not see the hook's exception
                result = wrapped.send_tool_call("crm.fetch", {"id": "1"})
                assert result == {"data": "ok"}

        # Defaults used despite the hook raising
        ctx = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")[0]
        assert ctx.payload.data_classifications == []
        assert ctx.payload.classification_source == ClassificationSource.unclassified

        # One bounded warning log line
        hook_warnings = [
            rec for rec in caplog.records
            if "classification_hook raised" in rec.message
        ]
        assert len(hook_warnings) == 1
        msg = hook_warnings[0].getMessage()
        # MUST include: tool name, exception class, exception message
        assert "tool=crm.fetch" in msg
        assert "ValueError" in msg
        assert "synthetic hook failure" in msg
        # MUST NOT include: arguments (would leak PII/credentials)
        assert "id" not in msg or "1" not in msg  # no argument leakage
        # Level must be WARNING, not ERROR
        assert hook_warnings[0].levelname == "WARNING"

    # ------------------------------------------------------------------
    # D5.6: tool raises -> only SCOPE_ASSERTED, no hook call, propagates
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_tool_exception_emits_only_scope_asserted(self):
        """Failure path acceptance criteria (MUST-level):
            - Only SCOPE_ASSERTED has been emitted
            - CONTEXT_SNAPSHOT MUST NOT be emitted
            - MEMORY_WRITE_ATTEMPT MUST NOT be emitted
            - classification_hook MUST NOT be invoked
            - Exception MUST propagate unchanged
        """
        hook_call_log: List[str] = []

        def hook(tool_name: str, args: dict, result: Any) -> ClassificationHint:
            hook_call_log.append(tool_name)  # hook must never record anything
            return ClassificationHint(data_classifications=["internal"])

        captured, wrapped = _make_test_client_and_hook_context(
            tool_raises=RuntimeError("synthetic tool failure"),
            classification_hook=hook,
        )

        async with wrapped:
            with pytest.raises(RuntimeError, match="synthetic tool failure"):
                wrapped.send_tool_call("crm.fetch", {"id": "1"})

        # Hook must NOT have been called at all
        assert hook_call_log == []

        # Events emitted before session end: AGENT_REGISTERED, INTENT_DECLARED,
        # SCOPE_ASSERTED. No CONTEXT_SNAPSHOT, no MEMORY_WRITE_ATTEMPT.
        ctx_events = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")
        mem_events = _events_by_type(captured.events, "MEMORY_WRITE_ATTEMPT")
        scope_events = _events_by_type(captured.events, "SCOPE_ASSERTED")

        assert len(scope_events) == 1
        assert len(ctx_events) == 0
        assert len(mem_events) == 0

    # ------------------------------------------------------------------
    # D5.7: memory write injection path
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_memory_write_hint_injection(self):
        """When the tool is a persistence target, hint fields write_classification
        and retention_requested must be honoured in the MEMORY_WRITE_ATTEMPT
        event."""

        def hook(tool_name: str, args: dict, result: Any) -> ClassificationHint:
            return ClassificationHint(
                data_classifications=["internal"],
                classification_source=ClassificationSource.vendor,
                write_classification="internal",
                retention_requested="30_days",
            )

        # Tool name must match the persistence-target heuristic.
        # _PERSISTENCE_KEYWORDS includes "vector_store".
        captured = _CapturingSink()

        class FakeMCPClient(MCPClientLike):
            def send_tool_call(self, tool_name: str, arguments: dict) -> Any:
                return {"ok": True}

        sm = SessionManager()
        cache = InProcessCache()
        sink = SinkWriter(captured)

        wrapped = wrap_mcp_client(
            target=FakeMCPClient(),
            session_manager=sm,
            cache=cache,
            sink_writer=sink,
            agent_id="mem-test-agent",
            stated_objective="Exercise memory write hint",
            declared_capabilities=["vector_store.upsert"],
            session_id="sess-mem-test",
            classification_hook=hook,
        )

        async with wrapped:
            wrapped.send_tool_call("vector_store.upsert", {"row": {"x": 1}})

        mem_events = _events_by_type(captured.events, "MEMORY_WRITE_ATTEMPT")
        assert len(mem_events) == 1
        mem = mem_events[0]
        assert mem.payload.write_classification == "internal"
        assert mem.payload.retention_requested == "30_days"
        assert mem.payload.target_store == "vector_store"
        # write_size_tokens reflects the write PAYLOAD (arguments), not
        # the response. That's the spec: a write event's "size" is what
        # was persisted.
        assert isinstance(mem.payload.write_size_tokens, int)
        assert mem.payload.write_size_tokens > 0


# ---------------------------------------------------------------------------
# D5 bonus: None vs intentional empty — load-bearing semantic distinction
# ---------------------------------------------------------------------------


class TestNoneVsIntentionalEmpty:
    """Pins the subtlest part of the hook contract: a hook that explicitly
    returns an empty list is semantically different from a hook that
    returns None, even though both currently produce the same serialized
    field value. If this distinction ever collapses (e.g. someone replaces
    _pick with ``value or default``), these tests fail."""

    @pytest.mark.asyncio
    async def test_hint_with_none_fields_uses_defaults(self):
        """Every field in the hint is None → wrapper uses its defaults for
        every field. Provenance default is [target_system]."""

        def hook(tool_name: str, args: dict, result: Any) -> ClassificationHint:
            return ClassificationHint()  # every field None

        captured, wrapped = _make_test_client_and_hook_context(
            classification_hook=hook,
        )
        async with wrapped:
            wrapped.send_tool_call("crm.fetch", {"id": "1"})

        ctx = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")[0]
        # provenance default is [target_system] = ["crm"]
        assert ctx.payload.provenance == ["crm"]

    @pytest.mark.asyncio
    async def test_hint_with_explicit_empty_list_overrides_default(self):
        """Hint explicitly sets provenance=[] → wrapper must emit [] (the
        caller's intentional value), not fall back to the [target_system]
        default."""

        def hook(tool_name: str, args: dict, result: Any) -> ClassificationHint:
            return ClassificationHint(provenance=[])  # explicit empty

        captured, wrapped = _make_test_client_and_hook_context(
            classification_hook=hook,
        )
        async with wrapped:
            wrapped.send_tool_call("crm.fetch", {"id": "1"})

        ctx = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")[0]
        # Explicit empty list must be honoured; MUST NOT fall back to ["crm"]
        assert ctx.payload.provenance == []


# ---------------------------------------------------------------------------
# Wrapper-level structural acceptance
# ---------------------------------------------------------------------------


class TestWrapperAcceptance:
    """Structural wrapper-level acceptance proof.

    This is NOT a byte-for-byte golden trace acceptance test. The
    canonical byte-for-byte proof lives at the EventBuilder layer in
    ``tests/test_golden_trace_acceptance.py`` (commits f8384c0 and
    0b27d3e), where every field of Flow A and Flow B is reproduced
    from the canonical reference.

    What this class proves instead:
        * The wrapper's PUBLIC API (``wrap_mcp_client``) can drive a
          clean end-to-end session through ``SessionManager``,
          ``InProcessCache``, ``EventBuilder``, and the sink writer.
        * The new event ordering contract holds: SCOPE_ASSERTED fires
          before the tool call; CONTEXT_SNAPSHOT and
          MEMORY_WRITE_ATTEMPT fire after. The ``classification_hook``
          receives the tool response (not the arguments), which is
          only possible with the post-call reorder.
        * Session-start events (AGENT_REGISTERED, INTENT_DECLARED)
          fire once per ``async with wrapped:`` block and carry the
          metadata supplied to ``wrap_mcp_client``.
        * ``event_sequence_number`` is monotonic across the whole
          session and ``previous_event_id`` forms an unbroken chain.
        * Clean-trace invariants hold: no advisory flags, no policy
          violations, ``pass_through=True`` on every event when the
          hook supplies proper classification metadata.
        * Hint fields flow into the correct emitted event fields for
          both CONTEXT_SNAPSHOT (data_classifications,
          classification_source, provenance, retention_flags,
          context_size_tokens) and MEMORY_WRITE_ATTEMPT
          (write_classification, retention_requested).

    What this class does NOT prove:
        * That the wrapper can reproduce Flow A or Flow B byte-for-byte
          (that is the builder-layer test's job; see rationale in).
        * That specific target_store / write_type / detection_mechanism
          values match Flow A's hand-authored values (the wrapper's
          persistence-target heuristic and hardcoded write_type are
          tracked separately; they are not gaps this test closes).

    Known wrapper-layer artifact — MEMORY_WRITE_CANDIDATE:
        The wrapper always emits MEMORY_WRITE_ATTEMPT events with
        ``write_type = write_to_persistence_target`` because it has
        no hook for a caller to declare a write as ``explicit_persist``.
        The EventBuilder fires the ``MEMORY_WRITE_CANDIDATE`` advisory
        flag whenever write_type is ``write_to_persistence_target``
        (see sentience_governor/event_builder/builder.py line 447).

        Therefore: every MEMORY_WRITE_ATTEMPT event produced by the
        wrapper carries ``advisory_flags = ["MEMORY_WRITE_CANDIDATE"]``,
        regardless of what the classification_hook supplies. This is
        NOT a policy violation — it is an advisory "you might want to
        declare this explicitly" nudge. The wrapper-layer tests
        acknowledge this artifact and assert on it explicitly.

        This gap will be closed when a ``write_type`` field is added
        to ``ClassificationHint``, allowing callers to declare
        ``explicit_persist`` via the hook. That is feature expansion
        beyond the current plan and is tracked separately.
    """

    # Helper: classify everything as internal/vendor for both context and
    # memory write fields. Returns the same hint regardless of tool — in a
    # real integration the hook would read metadata from the tool response.
    @staticmethod
    def _clean_classification_hook(hook_calls: list) -> ClassificationHook:
        def hook(tool_name: str, args: dict, result: Any) -> ClassificationHint:
            hook_calls.append(
                {"tool": tool_name, "args": args, "result": result}
            )
            return ClassificationHint(
                data_classifications=["internal"],
                classification_source=ClassificationSource.vendor,
                provenance=["acme"],
                retention_flags=["may-persist"],
                write_classification="internal",
                retention_requested="30_days",
            )
        return hook

    @pytest.mark.asyncio
    async def test_single_persistence_write_produces_clean_5_event_trace(self):
        """A session with exactly one tool call to a persistence target
        produces five events: AGENT_REGISTERED, INTENT_DECLARED,
        SCOPE_ASSERTED, CONTEXT_SNAPSHOT, MEMORY_WRITE_ATTEMPT.

        All five must be clean (no flags, no violations, pass_through=True)
        when the hook supplies proper classification metadata. The hook
        must have been called exactly once with the tool's response
        (proving the post-call reorder).
        """
        captured = _CapturingSink()
        hook_calls: list = []
        hook = self._clean_classification_hook(hook_calls)

        tool_result = {
            "data": {"records_upserted": 42},
            "status": "ok",
        }

        class FakeMCPClient(MCPClientLike):
            def send_tool_call(self, tool_name: str, arguments: dict) -> Any:
                return tool_result

        sm = SessionManager()
        cache = InProcessCache()
        sink = SinkWriter(captured)

        wrapped = wrap_mcp_client(
            target=FakeMCPClient(),
            session_manager=sm,
            cache=cache,
            sink_writer=sink,
            agent_id="reporting-agent-v1",
            agent_version="1.0.4",
            vendor_id="acme-analytics",
            declared_capabilities=["vector_store.write"],
            owner_claim="user_123",
            stated_objective="Store Q1 usage snapshot for Acme Corp",
            session_id="sess-wrapper-accept-001",
            classification_hook=hook,
        )

        async with wrapped:
            result = wrapped.send_tool_call(
                "vector_store.upsert",
                {"row": {"id": "acme-q1-2026", "usage": 12450}},
            )
            assert result is tool_result  # wrapper returns result unmodified

        events = captured.events

        # --- Event count and type sequence ---
        assert len(events) == 5, f"expected 5 events, got {len(events)}"
        types = [e.event_type.value for e in events]
        assert types == [
            "AGENT_REGISTERED",
            "INTENT_DECLARED",
            "SCOPE_ASSERTED",
            "CONTEXT_SNAPSHOT",
            "MEMORY_WRITE_ATTEMPT",
        ]

        # --- Sequence numbers monotonic, starting at 1 ---
        for i, event in enumerate(events, start=1):
            assert event.event_sequence_number == i

        # --- previous_event_id chain intact ---
        assert events[0].previous_event_id is None
        for i in range(1, len(events)):
            assert events[i].previous_event_id == events[i - 1].event_id

        # --- Clean trace contract ---
        # Policy violations: zero across every event. This is the real
        # "clean governance" claim — the runtime is not flagging the
        # agent's behaviour as against policy.
        for event in events:
            assert event.policy_violations == [], (
                f"unexpected policy violation on {event.event_type.value}: "
                f"{event.policy_violations}"
            )
            assert event.pass_through is True

        # Advisory flags: zero on all events EXCEPT MEMORY_WRITE_ATTEMPT,
        # which carries the MEMORY_WRITE_CANDIDATE wrapper artifact (see
        # class docstring). This is not a regression — it's the wrapper's
        # hardcoded write_type=write_to_persistence_target producing an
        # advisory nudge from the builder.
        for event in events:
            if event.event_type.value == "MEMORY_WRITE_ATTEMPT":
                assert event.advisory_flags == ["MEMORY_WRITE_CANDIDATE"], (
                    f"memory write event has unexpected advisory flags: "
                    f"{event.advisory_flags}"
                )
            else:
                assert event.advisory_flags == [], (
                    f"unexpected advisory flags on {event.event_type.value}: "
                    f"{event.advisory_flags}"
                )

        # --- Hook was called exactly once, with the RESPONSE ---
        assert len(hook_calls) == 1
        assert hook_calls[0]["tool"] == "vector_store.upsert"
        assert hook_calls[0]["args"] == {"row": {"id": "acme-q1-2026", "usage": 12450}}
        # The hook receiving result IS the proof the reorder happened —
        # a pre-call hook could not possibly have the response yet.
        assert hook_calls[0]["result"] is tool_result

        # --- CONTEXT_SNAPSHOT hint fields honoured ---
        ctx = events[3]
        assert ctx.event_type.value == "CONTEXT_SNAPSHOT"
        assert ctx.payload.data_classifications == ["internal"]
        assert ctx.payload.classification_source == ClassificationSource.vendor
        assert ctx.payload.provenance == ["acme"]
        assert ctx.payload.retention_flags == ["may-persist"]
        assert isinstance(ctx.payload.context_size_tokens, int)
        assert ctx.payload.context_size_tokens > 0

        # --- MEMORY_WRITE_ATTEMPT hint fields honoured ---
        mem = events[4]
        assert mem.event_type.value == "MEMORY_WRITE_ATTEMPT"
        assert mem.payload.write_classification == "internal"
        assert mem.payload.retention_requested == "30_days"
        assert mem.payload.target_store == "vector_store"
        assert isinstance(mem.payload.write_size_tokens, int)
        assert mem.payload.write_size_tokens > 0

        # --- Session-start metadata carried through ---
        reg = events[0]
        assert reg.payload.agent_id == "reporting-agent-v1"
        assert reg.payload.agent_version == "1.0.4"
        assert reg.payload.vendor_id == "acme-analytics"
        assert reg.payload.owner_claim == "user_123"

        intent = events[1]
        assert intent.payload.stated_objective == "Store Q1 usage snapshot for Acme Corp"
        assert intent.payload.intent_source.value == "explicit"

    @pytest.mark.asyncio
    async def test_multi_call_read_then_write_produces_clean_7_event_trace(self):
        """A session with a read call followed by a persistence write call
        produces seven events: 2 session-start + 2 for the read (SCOPE +
        CONTEXT) + 3 for the write (SCOPE + CONTEXT + MEMORY).

        Proves the wrapper handles multi-call sessions with correct
        per-call event emission and intact sequence / chain integrity
        across call boundaries.
        """
        captured = _CapturingSink()
        hook_calls: list = []
        hook = self._clean_classification_hook(hook_calls)

        class FakeMCPClient(MCPClientLike):
            def send_tool_call(self, tool_name: str, arguments: dict) -> Any:
                if tool_name == "crm.fetch_usage":
                    return {"customer": "acme", "usage": 12450}
                if tool_name == "vector_store.upsert":
                    return {"ok": True}
                raise ValueError(f"unexpected tool: {tool_name}")

        sm = SessionManager()
        cache = InProcessCache()
        sink = SinkWriter(captured)

        wrapped = wrap_mcp_client(
            target=FakeMCPClient(),
            session_manager=sm,
            cache=cache,
            sink_writer=sink,
            agent_id="reporting-agent-v1",
            agent_version="1.0.4",
            vendor_id="acme-analytics",
            declared_capabilities=["crm.read", "vector_store.write"],
            owner_claim="user_123",
            stated_objective="Fetch Acme usage and cache it",
            session_id="sess-wrapper-accept-002",
            classification_hook=hook,
        )

        async with wrapped:
            wrapped.send_tool_call("crm.fetch_usage", {"customer_id": "acme"})
            wrapped.send_tool_call(
                "vector_store.upsert",
                {"row": {"customer": "acme", "usage": 12450}},
            )

        events = captured.events

        # --- Event count: 2 session-start + 2 read + 3 write = 7 ---
        assert len(events) == 7, f"expected 7 events, got {len(events)}"

        # --- Event type sequence ---
        types = [e.event_type.value for e in events]
        assert types == [
            "AGENT_REGISTERED",
            "INTENT_DECLARED",
            "SCOPE_ASSERTED",       # read call
            "CONTEXT_SNAPSHOT",     # read call (post-call)
            "SCOPE_ASSERTED",       # write call
            "CONTEXT_SNAPSHOT",     # write call (post-call)
            "MEMORY_WRITE_ATTEMPT", # write call (persistence target)
        ]

        # --- Sequence numbers monotonic across the whole session ---
        for i, event in enumerate(events, start=1):
            assert event.event_sequence_number == i

        # --- previous_event_id chain spans call boundaries ---
        assert events[0].previous_event_id is None
        for i in range(1, len(events)):
            assert events[i].previous_event_id == events[i - 1].event_id

        # --- All events governance-clean (zero policy violations) ---
        # Same pattern as the single-call test: MEMORY_WRITE_ATTEMPT
        # carries the MEMORY_WRITE_CANDIDATE wrapper artifact; nothing
        # else has any flag.
        for event in events:
            assert event.policy_violations == [], (
                f"unexpected policy violation on {event.event_type.value}: "
                f"{event.policy_violations}"
            )
            assert event.pass_through is True
            if event.event_type.value == "MEMORY_WRITE_ATTEMPT":
                assert event.advisory_flags == ["MEMORY_WRITE_CANDIDATE"], (
                    f"memory write event has unexpected advisory flags: "
                    f"{event.advisory_flags}"
                )
            else:
                assert event.advisory_flags == [], (
                    f"unexpected advisory flags on {event.event_type.value}: "
                    f"{event.advisory_flags}"
                )

        # --- Hook was called exactly twice, once per tool call,
        #     each with the correct response ---
        assert len(hook_calls) == 2
        assert hook_calls[0]["tool"] == "crm.fetch_usage"
        assert hook_calls[0]["result"] == {"customer": "acme", "usage": 12450}
        assert hook_calls[1]["tool"] == "vector_store.upsert"
        assert hook_calls[1]["result"] == {"ok": True}

        # --- Both CONTEXT_SNAPSHOT events carry the hint's classification ---
        ctx_events = _events_by_type(events, "CONTEXT_SNAPSHOT")
        assert len(ctx_events) == 2
        for ctx in ctx_events:
            assert ctx.payload.data_classifications == ["internal"]
            assert ctx.payload.classification_source == ClassificationSource.vendor
            assert ctx.payload.provenance == ["acme"]

        # --- The single MEMORY_WRITE_ATTEMPT belongs to the write call ---
        mem_events = _events_by_type(events, "MEMORY_WRITE_ATTEMPT")
        assert len(mem_events) == 1
        mem = mem_events[0]
        assert mem.payload.target_store == "vector_store"
        assert mem.payload.write_classification == "internal"
        assert mem.payload.retention_requested == "30_days"


# ---------------------------------------------------------------------------
# v0.2.3 Track 2 — token-tracking passthrough
# ---------------------------------------------------------------------------


class TestTokenFieldPassthrough:
    """The wrapper does not extract token data — it just passes the
    fields the hook populates through to the emitted CONTEXT_SNAPSHOT.
    These tests verify that passthrough."""

    @pytest.mark.asyncio
    async def test_hook_populated_token_fields_appear_in_event(self):
        def hook(tool_name, arguments, result):
            return ClassificationHint(
                llm_prompt_tokens=100,
                llm_completion_tokens=50,
                llm_cached_read_tokens=20,
                model_identifier="claude-sonnet-4-5",
                provider="anthropic",
                llm_turn_id="turn-abc",
            )

        captured, wrapped = _make_test_client_and_hook_context(classification_hook=hook)
        async with wrapped:
            wrapped.send_tool_call("crm.fetch", {"customer": "x"})

        ctx_events = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")
        assert ctx_events
        payload = ctx_events[-1].payload
        assert payload.llm_prompt_tokens == 100
        assert payload.llm_completion_tokens == 50
        assert payload.llm_cached_read_tokens == 20
        assert payload.model_identifier == "claude-sonnet-4-5"
        assert payload.provider == "anthropic"
        assert payload.llm_turn_id == "turn-abc"

    @pytest.mark.asyncio
    async def test_no_hook_yields_all_none_token_fields(self):
        captured, wrapped = _make_test_client_and_hook_context(classification_hook=None)
        async with wrapped:
            wrapped.send_tool_call("crm.fetch", {"customer": "x"})

        ctx_events = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")
        payload = ctx_events[-1].payload
        # All-None on the model; serialization drops them.
        assert payload.llm_prompt_tokens is None
        assert payload.llm_turn_id is None
        # None fields omitted from serialised dict.
        d = payload.model_dump()
        assert "llm_prompt_tokens" not in d
        assert "llm_turn_id" not in d

    @pytest.mark.asyncio
    async def test_hook_with_zero_token_field_preserved(self):
        def hook(tool_name, arguments, result):
            return ClassificationHint(llm_prompt_tokens=0)

        captured, wrapped = _make_test_client_and_hook_context(classification_hook=hook)
        async with wrapped:
            wrapped.send_tool_call("crm.fetch", {"customer": "x"})
        ctx_events = _events_by_type(captured.events, "CONTEXT_SNAPSHOT")
        payload = ctx_events[-1].payload
        assert payload.llm_prompt_tokens == 0
        # Zero appears in the dict; not omitted.
        d = payload.model_dump()
        assert d["llm_prompt_tokens"] == 0
