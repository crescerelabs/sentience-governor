"""LangChain Path B — SentienceCallbackHandler and SentienceMiddleware.

Mapping
-------
on_chain_start  → AGENT_REGISTERED
on_tool_start   → SCOPE_ASSERTED + CONTEXT_SNAPSHOT
on_tool_end     → CONTEXT_SNAPSHOT (update)
awrap_tool_call → observe only, never blocks

Intent capture
--------------
* From agent inputs or system prompts → INTENT_DECLARED as early as possible.
* Falls back to intent_source=none if no signal.

v0 constraint: observe only.  SentienceMiddleware never blocks.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Union

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile import GovernanceProfile
from sentience_governor.schema.events import (
    ClassificationSource,
    DeploymentMode,
    IntentConfidence,
    IntentSource,
    OperationType,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import SinkWriter
from sentience_governor.wrapper.token_extraction import (
    CANONICAL_TOKEN_FIELDS,
    extract_from_langchain_response,
)

logger = logging.getLogger(__name__)


class SentienceCallbackHandler:
    """Maps LangChain callback events to governance control points.

    Attach as a callback handler to a LangChain agent or chain.
    This class does not inherit from BaseCallbackHandler to avoid a hard
    dependency on langchain-core at import time; it implements the same
    interface via duck typing.

    If langchain-core is available, you may subclass BaseCallbackHandler:

        from langchain_core.callbacks import BaseCallbackHandler
        class MySentienceHandler(SentienceCallbackHandler, BaseCallbackHandler): ...
    """

    def __init__(
        self,
        agent_id: str,
        session_manager: SessionManager,
        cache: InProcessCache,
        sink_writer: SinkWriter,
        deployment_mode: DeploymentMode = DeploymentMode.vendor_managed,
        agent_version: Optional[str] = None,
        vendor_id: Optional[str] = None,
        declared_capabilities: Optional[List[str]] = None,
        owner_claim: Optional[str] = None,
    ) -> None:
        self._agent_id = agent_id
        self._sm = session_manager
        self._cache = cache
        self._sink = sink_writer
        self._deployment_mode = deployment_mode
        self._agent_version = agent_version
        self._vendor_id = vendor_id
        self._declared_capabilities = declared_capabilities or []
        self._owner_claim = owner_claim
        self._session_id: Optional[str] = None
        self._intent_emitted: bool = False
        self._builder: Optional[EventBuilder] = None

        # v0.2.3 Track 2 — per-LLM-turn pending state. Lifecycle:
        #   on_llm_start  -> reset everything, allocate new turn id
        #   on_llm_end    -> populate _pending_llm_usage / model / provider
        #   on_tool_start -> read pending state, attach to emitted event
        #
        # Concurrency assumption (per plan §3.0.1): one handler instance
        # per agent run / session. Sharing a single instance across
        # concurrent runs would cause cross-run leakage of pending state.
        # If shared instances are needed in the future, switch to
        # contextvars.ContextVar — out of v0.2.3 scope.
        #
        # Trace immutability (per plan §3.2): if on_tool_start fires
        # before on_llm_end has populated usage, the emitted event
        # carries the current turn id (if on_llm_start ran) but NO
        # token fields. We never mutate already-emitted events.
        self._pending_llm_usage: Dict[str, Optional[int]] = {
            field: None for field in CANONICAL_TOKEN_FIELDS
        }
        self._pending_llm_model: Optional[str] = None
        self._pending_llm_provider: Optional[str] = None
        self._pending_llm_turn_id: Optional[str] = None

    # ------------------------------------------------------------------
    # LangChain callback surface
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Map to AGENT_REGISTERED; capture intent from inputs."""
        self._session_id = str(uuid.uuid4())
        # v0.2.5: load operator-authored governance profile if present.
        # See mcp.py._start for full rationale.
        profile = GovernanceProfile.from_default_path_or_none()
        self._sm.session_start(
            session_id=self._session_id,
            agent_id=self._agent_id,
            profile=profile,
        )
        self._cache.init_session(self._session_id)
        self._builder = EventBuilder(
            session_manager=self._sm,
            cache=self._cache,
            agent_id=self._agent_id,
            session_id=self._session_id,
            deployment_mode=self._deployment_mode,
        )

        # Emit AGENT_REGISTERED
        event = self._builder.build_agent_registered(
            agent_version=self._agent_version,
            vendor_id=self._vendor_id,
            declared_capabilities=self._declared_capabilities,
            owner_claim=self._owner_claim,
        )
        if event:
            self._sink.write(event, self._session_id)

        # Attempt to capture intent from inputs
        stated_objective = self._extract_intent(inputs)
        self._emit_intent(stated_objective)

    def on_chain_end(self, outputs: Dict[str, Any], **kwargs: Any) -> None:
        if not self._intent_emitted and self._builder:
            self._emit_intent(None)
        if self._session_id:
            self._sink.session_closed(self._session_id, self._agent_id)
            self._sm.session_end(self._session_id)
            self._cache.clear_session(self._session_id)

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: Any,
        **kwargs: Any,
    ) -> None:
        """v0.2.3 Track 2 — turn-boundary reset.

        Fires at the START of every LLM turn. Resets all pending state
        from any prior turn so the new turn starts clean, and allocates
        a fresh ``llm_turn_id`` that subsequent ``on_tool_start`` events
        in this turn will attach to their emitted CONTEXT_SNAPSHOT.

        ``on_llm_end`` later in this turn populates the usage fields
        without regenerating the turn id (the same id must persist
        across all events from this turn so downstream consumers can
        dedupe correctly — see plan §1.4 aggregation contract).
        """
        self._pending_llm_usage = {field: None for field in CANONICAL_TOKEN_FIELDS}
        self._pending_llm_model = None
        self._pending_llm_provider = None
        self._pending_llm_turn_id = uuid.uuid4().hex

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """v0.2.3 Track 2 — capture token usage from the LLM turn that's just ended.

        Stores extracted canonical fields in instance state so any
        subsequent ``on_tool_start`` events fired in this turn can
        attach the same usage AND the same ``llm_turn_id`` (allocated
        by ``on_llm_start``) to their emitted CONTEXT_SNAPSHOT.

        Defensive: any exception inside the extraction helper is
        swallowed and pending usage falls back to all-None. Token
        extraction must never break the agent's primary work.

        Note: we do NOT regenerate ``_pending_llm_turn_id`` here — it
        was set by ``on_llm_start`` and must persist across every event
        produced from this turn.
        """
        try:
            self._pending_llm_usage = extract_from_langchain_response(response)
        except Exception:
            logger.debug(
                "extract_from_langchain_response raised; falling back to all-None",
                exc_info=True,
            )
            self._pending_llm_usage = {
                field: None for field in CANONICAL_TOKEN_FIELDS
            }

        # Defensive: model / provider extraction also exception-safe.
        # A response object that explodes on getattr (custom __getattr__,
        # mocking gone wrong) must not break the handler.
        try:
            self._pending_llm_model = self._extract_model_from_response(response)
        except Exception:
            logger.debug(
                "_extract_model_from_response raised; falling back to None",
                exc_info=True,
            )
            self._pending_llm_model = None
        try:
            self._pending_llm_provider = self._extract_provider_from_response(response)
        except Exception:
            logger.debug(
                "_extract_provider_from_response raised; falling back to None",
                exc_info=True,
            )
            self._pending_llm_provider = None

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Emit INTENT_DECLARED (if not yet), SCOPE_ASSERTED, CONTEXT_SNAPSHOT."""
        if not self._intent_emitted and self._builder:
            self._emit_intent(None)

        if not self._builder:
            return

        tool_name = serialized.get("name", "unknown_tool")
        operation_type = self._infer_operation_type(tool_name, input_str)

        # SCOPE_ASSERTED
        scope_event = self._builder.build_scope_asserted(
            tool_id=tool_name,
            asserted_permissions=[operation_type.value.lower()],
            target_system=self._extract_target_system(tool_name),
            operation_type=operation_type,
        )
        if scope_event:
            self._sink.write(scope_event, self._session_id)

        # CONTEXT_SNAPSHOT — at tool call with incoming data.
        # Attaches v0.2.3 Track 2 token-usage fields from the most
        # recent LLM turn (populated by on_llm_end). When on_llm_end
        # has not yet run for this turn (rare ordering — see plan §3.2
        # immutability rule), all token fields are None and only the
        # turn id (if on_llm_start ran) is attached. Already-emitted
        # events are NEVER mutated retroactively.
        ctx_event = self._builder.build_context_snapshot(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=[tool_name],
            retention_flags=[],
            context_size_tokens=len(input_str.split()) * 2,
            **self._token_kwargs(),
        )
        if ctx_event:
            self._sink.write(ctx_event, self._session_id)

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        """Update CONTEXT_SNAPSHOT with tool output (classification unknown)."""
        if not self._builder:
            return
        ctx_event = self._builder.build_context_snapshot(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=["tool_output"],
            retention_flags=[],
            context_size_tokens=len(str(output).split()) * 2,
            # Same turn id + usage — every event in the turn carries
            # the same attribution. Consumers dedupe by
            # (session_id, llm_turn_id) before summing token fields.
            **self._token_kwargs(),
        )
        if ctx_event:
            self._sink.write(ctx_event, self._session_id)

    # ------------------------------------------------------------------
    # v0.2.3 Track 2 — token-attribution helpers
    # ------------------------------------------------------------------

    def _token_kwargs(self) -> Dict[str, Any]:
        """Build keyword args for build_context_snapshot from pending state.

        Returns the 5 numeric token fields + model + provider + turn id.
        Any field set to None on the result is dropped from the payload
        by the central serializer (per plan §1.3); no per-call filtering
        needed here.
        """
        return {
            **self._pending_llm_usage,
            "model_identifier": self._pending_llm_model,
            "provider": self._pending_llm_provider,
            "llm_turn_id": self._pending_llm_turn_id,
        }

    @staticmethod
    def _extract_model_from_response(response: Any) -> Optional[str]:
        """Best-effort extraction of model identifier from a LangChain response.

        Probes common attributes; returns None if nothing matches.
        """
        # Newer LC: AIMessage.response_metadata['model_name']
        response_metadata = getattr(response, "response_metadata", None) or {}
        if isinstance(response_metadata, dict):
            for key in ("model_name", "model", "model_id"):
                value = response_metadata.get(key)
                if isinstance(value, str) and value:
                    return value
        # Direct attribute
        for attr in ("model", "model_name", "model_id"):
            value = getattr(response, attr, None)
            if isinstance(value, str) and value:
                return value
        # Older LC: response.llm_output['model_name']
        llm_output = getattr(response, "llm_output", None) or {}
        if isinstance(llm_output, dict):
            for key in ("model_name", "model", "model_id"):
                value = llm_output.get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _extract_provider_from_response(response: Any) -> Optional[str]:
        """Best-effort extraction of provider name from a LangChain response.

        We don't sniff hard — most responses don't carry an explicit
        provider field. Adopters who care should populate this on the
        hint themselves.
        """
        # response_metadata sometimes carries a provider hint
        response_metadata = getattr(response, "response_metadata", None) or {}
        if isinstance(response_metadata, dict):
            value = response_metadata.get("provider")
            if isinstance(value, str) and value:
                return value
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_intent(
        self,
        stated_objective: Optional[str],
        source: IntentSource = IntentSource.inferred,
    ) -> None:
        """Emit the INTENT_DECLARED event for this session.

        Parameters
        ----------
        stated_objective
            The objective string. If ``None``, the emitted event carries
            ``intent_source=none`` regardless of the ``source`` argument.
        source
            Where the objective came from. Defaults to
            ``IntentSource.inferred`` because the only call site that
            supplies a non-None ``stated_objective`` is ``on_chain_start``,
            which extracts the string from the chain's invocation inputs
            at runtime. Callers that want to declare an integrator-supplied
            objective must pass ``source=IntentSource.explicit`` explicitly.

        Confidence semantics
        --------------------
        Confidence reflects epistemic trust in the *content* of the
        objective, not the reliability of the *extraction mechanism*.

        * ``source=IntentSource.explicit`` -> ``IntentConfidence.explicit``
          (integrator declared the objective at wrapper construction time;
          high-trust content, high-trust mechanism)
        * ``source=IntentSource.inferred`` -> ``IntentConfidence.inferred_low``
          (extraction mechanism is reliable, but the meaning of the
          extracted string is opaque — it could be a user request, a
          machine-generated payload, or anything in between)
        * No objective supplied -> ``IntentConfidence.unknown``
        """
        if self._intent_emitted or not self._builder:
            return
        if stated_objective:
            confidence = (
                IntentConfidence.explicit
                if source is IntentSource.explicit
                else IntentConfidence.inferred_low
            )
        else:
            source = IntentSource.none
            confidence = IntentConfidence.unknown

        event = self._builder.build_intent_declared(
            stated_objective=stated_objective,
            intent_source=source,
            intent_confidence=confidence,
            authorization_claim=self._owner_claim,
            session_scope_hint=self._declared_capabilities,
        )
        if event:
            self._sink.write(event, self._session_id)
        self._intent_emitted = True

    @staticmethod
    def _extract_intent(inputs: Dict[str, Any]) -> Optional[str]:
        for key in ("input", "question", "objective", "task", "prompt"):
            if key in inputs and isinstance(inputs[key], str):
                return inputs[key]
        return None

    @staticmethod
    def _infer_operation_type(tool_name: str, input_str: str) -> OperationType:
        name_lower = tool_name.lower()
        if any(k in name_lower for k in ("write", "update", "create", "insert", "put")):
            return OperationType.WRITE
        if any(k in name_lower for k in ("delete", "remove", "drop")):
            return OperationType.DELETE
        if any(k in name_lower for k in ("exec", "run", "execute")):
            return OperationType.EXECUTE
        return OperationType.READ

    @staticmethod
    def _extract_target_system(tool_name: str) -> str:
        parts = tool_name.split(".")
        return parts[0] if len(parts) > 1 else tool_name


class SentienceMiddleware:
    """Wraps tool calls via awrap_tool_call — observe only, never blocks.

    Usage (conceptual):
        middleware = SentienceMiddleware(handler)
        agent = create_react_agent(..., middleware=[middleware])

    v0.2.3 Track 2 also provides ``awrap_step`` for users on
    ``create_react_agent`` (LangGraph) shapes who want token-usage
    aggregation across messages within a step. ``awrap_tool_call``
    stays backward-compatible: users who don't register
    ``awrap_step`` see token fields stay ``None`` for tool-call events
    emitted through this middleware (the underlying handler still
    reports tokens through ``on_llm_end`` if that hook is wired to
    the same handler).
    """

    def __init__(self, handler: SentienceCallbackHandler) -> None:
        self._handler = handler

        # v0.2.3 Track 2 — per-LangGraph-step state. Lifecycle owned by
        # awrap_step (set on entry, read by awrap_tool_call while the
        # step is in flight, cleared in awrap_step's finally block).
        # Concurrency note (per plan §3.0.1): one middleware instance
        # per agent run. Sharing across concurrent runs would clobber
        # this state — switch to contextvars.ContextVar if that ever
        # becomes a real usage pattern.
        self._current_step_usage: Optional[Dict[str, Optional[int]]] = None
        self._current_step_model: Optional[str] = None
        self._current_step_provider: Optional[str] = None
        self._current_step_turn_id: Optional[str] = None

    async def awrap_tool_call(
        self,
        tool_name: str,
        tool_input: Any,
        next_call: Any,
    ) -> Any:
        """Intercept tool call; emit governance events; never block.

        If ``awrap_step`` has populated step state for the current
        LangGraph step, that state is mirrored onto the handler's
        pending fields BEFORE the handler's ``on_tool_start`` fires —
        so the emitted CONTEXT_SNAPSHOT carries the step-level
        aggregated tokens + step turn id. If no step state is set
        (handler used standalone, or step middleware not registered),
        the handler's existing pending state (from ``on_llm_end``,
        if wired) is used unchanged.
        """
        serialized = {"name": tool_name}
        input_str = str(tool_input)

        # If awrap_step has populated step state, mirror it to the
        # handler so the emitted event picks it up via the existing
        # _token_kwargs() pathway. We snapshot+restore so this never
        # leaks across calls when step state isn't set.
        snapshot = self._snapshot_handler_pending_state()
        try:
            if self._current_step_turn_id is not None:
                self._handler._pending_llm_usage = (
                    self._current_step_usage
                    if self._current_step_usage is not None
                    else {field: None for field in CANONICAL_TOKEN_FIELDS}
                )
                self._handler._pending_llm_model = self._current_step_model
                self._handler._pending_llm_provider = self._current_step_provider
                self._handler._pending_llm_turn_id = self._current_step_turn_id

            self._handler.on_tool_start(serialized, input_str)
            try:
                result = await next_call(tool_input)
                self._handler.on_tool_end(str(result))
                return result
            except Exception:
                # Fail-open: governance failure must never block agent
                raise
        finally:
            # Restore handler state. Important when step state was set:
            # the next tool call in the same step should read step state
            # again from awrap_tool_call, NOT from a polluted handler.
            # Without this, a turn id leaked into the handler would
            # persist beyond the step's lifetime.
            self._restore_handler_pending_state(snapshot)

    async def awrap_step(
        self,
        state_in: Any,
        state_out: Any,
        next_call: Any,
    ) -> Any:
        """v0.2.3 Track 2 — fires once per LangGraph step.

        Walks the message delta from ``state_in`` to ``state_out``,
        extracts token usage from each new AIMessage, aggregates per
        plan §3.3.1, and stores the aggregated result in step state
        for ``awrap_tool_call`` to attach to emitted events.

        Lifecycle (load-bearing — see plan §3.3.2):
          * Step state is **set** at the top of this method, before
            ``next_call``.
          * Step state is **read** by ``awrap_tool_call`` during the
            step.
          * Step state is **cleared** in the ``finally`` block, so
            the next step starts clean and stale usage cannot leak
            across step boundaries.

        Aggregation rules (per §3.3.1):
          * Numeric token fields: sum across messages; ``None``
            contributions are skipped (NOT treated as zero); all-
            ``None`` aggregates to ``None``, not 0.
          * ``model_identifier`` and ``provider``: preserved only when
            consistent across all messages in the step; ``None`` when
            mixed (no magic ``"multiple"`` sentinel).
        """
        try:
            messages = self._extract_new_ai_messages(state_in, state_out)
            self._current_step_usage, self._current_step_model, \
                self._current_step_provider = self._aggregate_messages(messages)
            self._current_step_turn_id = uuid.uuid4().hex

            return await next_call(state_in)
        finally:
            # CRITICAL: clear after step completes so next step starts
            # clean. A stale turn id leaking across steps would silently
            # over-attribute later events.
            self._current_step_usage = None
            self._current_step_model = None
            self._current_step_provider = None
            self._current_step_turn_id = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _snapshot_handler_pending_state(self) -> Dict[str, Any]:
        return {
            "usage": dict(self._handler._pending_llm_usage),
            "model": self._handler._pending_llm_model,
            "provider": self._handler._pending_llm_provider,
            "turn_id": self._handler._pending_llm_turn_id,
        }

    def _restore_handler_pending_state(self, snapshot: Dict[str, Any]) -> None:
        self._handler._pending_llm_usage = snapshot["usage"]
        self._handler._pending_llm_model = snapshot["model"]
        self._handler._pending_llm_provider = snapshot["provider"]
        self._handler._pending_llm_turn_id = snapshot["turn_id"]

    @staticmethod
    def _extract_new_ai_messages(state_in: Any, state_out: Any) -> List[Any]:
        """Compute the AIMessage delta between two LangGraph states.

        LangGraph state shape varies; we probe ``messages`` attribute
        on both, then return any message in ``state_out.messages`` not
        already present in ``state_in.messages`` (by identity / index).

        Returns an empty list if neither state exposes ``messages`` —
        the caller treats that as "no new messages" and emits all-None
        token fields.
        """
        in_messages = getattr(state_in, "messages", None)
        out_messages = getattr(state_out, "messages", None)
        if not isinstance(in_messages, list) and isinstance(state_in, dict):
            in_messages = state_in.get("messages")
        if not isinstance(out_messages, list) and isinstance(state_out, dict):
            out_messages = state_out.get("messages")

        in_messages = in_messages or []
        out_messages = out_messages or []

        # Simple delta: take messages beyond the input length. LangGraph
        # appends to messages so this is correct for the common case.
        # If both states share zero overlap, return all of out_messages.
        if len(out_messages) <= len(in_messages):
            return []
        new_messages = out_messages[len(in_messages):]

        # Filter to AIMessage-like objects. We don't import LangChain
        # types — duck-type by checking for ``response_metadata`` or
        # ``usage_metadata`` attributes that token extraction needs.
        return [
            msg for msg in new_messages
            if hasattr(msg, "response_metadata")
            or hasattr(msg, "usage_metadata")
            or hasattr(msg, "llm_output")
        ]

    @staticmethod
    def _aggregate_messages(
        messages: List[Any],
    ) -> tuple:
        """Aggregate token usage across messages per plan §3.3.1.

        Returns ``(usage_dict, model_identifier, provider)``.

        Sum semantics: for each canonical numeric field, sum the
        non-None values across messages. If all messages report None
        for that field, aggregate is None. Mixed None+int: sum the
        ints (None contributions skipped, NOT treated as zero).

        Identity semantics for model/provider: preserved only when
        consistent across all messages, otherwise None.
        """
        if not messages:
            return ({field: None for field in CANONICAL_TOKEN_FIELDS}, None, None)

        # Aggregate numeric fields.
        aggregated: Dict[str, Optional[int]] = {
            field: None for field in CANONICAL_TOKEN_FIELDS
        }
        for msg in messages:
            try:
                msg_usage = extract_from_langchain_response(msg)
            except Exception:
                continue
            for field in CANONICAL_TOKEN_FIELDS:
                value = msg_usage.get(field)
                if value is None:
                    continue
                current = aggregated[field]
                aggregated[field] = (current or 0) + value

        # Aggregate model/provider — preserve only if consistent.
        models = set()
        providers = set()
        for msg in messages:
            response_metadata = getattr(msg, "response_metadata", None) or {}
            if isinstance(response_metadata, dict):
                model = response_metadata.get("model_name") or response_metadata.get("model")
                if isinstance(model, str) and model:
                    models.add(model)
                provider = response_metadata.get("provider")
                if isinstance(provider, str) and provider:
                    providers.add(provider)
            for attr in ("model", "model_name", "model_id"):
                value = getattr(msg, attr, None)
                if isinstance(value, str) and value:
                    models.add(value)
                    break

        model_identifier = next(iter(models)) if len(models) == 1 else None
        provider = next(iter(providers)) if len(providers) == 1 else None

        return (aggregated, model_identifier, provider)
