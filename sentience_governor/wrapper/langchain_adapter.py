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

v0.3.0.2 — root-scoped governance sessions
------------------------------------------
LangGraph fires chain-level callbacks once per graph AND once per node, so
one ``.invoke()`` produces several ``on_chain_start`` / ``on_chain_end``
pairs.  Governance state is therefore keyed by *root invocation* rather
than held in single instance slots:

* ``on_chain_start`` creates a session only when ``parent_run_id is None``.
* ``on_chain_end`` tears down only the root whose ``run_id`` is ending.
* Tool and LLM callbacks resolve their root by walking ``parent_run_id``
  ancestry, because tool events parent to the *node*, not the root.
* LLM turn telemetry is scoped per *branch* inside a root, because two
  parallel nodes of one graph have overlapping LLM turns.

See design 0001 for the measurements behind each of those rules.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile import GovernanceProfile
from sentience_governor.schema.events import (
    ClassificationSource,
    DeploymentMode,
    ErrorType,
    EventType,
    GovernanceErrorPayload,
    GovernanceEvent,
    IntentConfidence,
    IntentSource,
    OperationType,
    PrimitiveType,
    Severity,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import SinkWriter
from sentience_governor.wrapper.token_extraction import (
    CANONICAL_TOKEN_FIELDS,
    extract_from_langchain_response,
)

logger = logging.getLogger(__name__)


class _Sentinel:
    """Named singleton, so debugging output is readable."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return self._name


#: Root key used when the caller supplies no ``run_id``/``parent_run_id``.
#: Direct callers of the callback surface (and the 32 pre-v0.3.0.2 adapter
#: tests) never pass callback ids; they get exactly today's single-session
#: behaviour through this key.  See design 0001 §4.5.
_LEGACY_ROOT = _Sentinel("<legacy-root>")

#: Returned by :meth:`SentienceCallbackHandler._bind_middleware_root` when
#: two or more roots are open and the middleware — which carries no callback
#: ids at all — cannot be attributed to one of them without guessing.
_MIDDLEWARE_AMBIGUOUS = _Sentinel("<middleware-ambiguous>")

#: Bound on an ancestry walk.  Guards against a malformed or cyclic parent
#: chain; it is not a correctness parameter of the model.
_MAX_HOPS = 64


@dataclass
class _TurnState:
    """Pending telemetry for ONE LLM turn, scoped to ONE branch.

    Lifecycle:
      on_llm_start  -> reset every field in place, allocate a new turn id
      on_llm_end    -> populate usage / model / provider, NEVER the turn id
      on_tool_*     -> read, attach to the emitted CONTEXT_SNAPSHOT

    Reset happens in place rather than by replacing the object so the
    legacy compatibility views (``_pending_llm_*``) keep observing the
    same scope across turns.
    """

    usage: Dict[str, Optional[int]] = field(
        default_factory=lambda: {f: None for f in CANONICAL_TOKEN_FIELDS}
    )
    model: Optional[str] = None
    provider: Optional[str] = None
    turn_id: Optional[str] = None

    def reset(self, turn_id: Optional[str]) -> None:
        self.usage = {f: None for f in CANONICAL_TOKEN_FIELDS}
        self.model = None
        self.provider = None
        self.turn_id = turn_id


@dataclass
class _RootState:
    """Governance state belonging to ONE root invocation.

    ``turn_scopes`` is keyed by branch — the ``parent_run_id`` shared by an
    LLM turn and the tool calls of the same node.  The ``None`` key is the
    root's default scope and is what flat chains and the legacy path use.
    """

    session_id: str
    builder: EventBuilder
    intent_emitted: bool = False
    turn_scopes: Dict[Any, _TurnState] = field(default_factory=dict)


class SentienceCallbackHandler:
    """Maps LangChain callback events to governance control points.

    Attach as a callback handler to a LangChain agent or chain.
    This class does not inherit from BaseCallbackHandler to avoid a hard
    dependency on langchain-core at import time; it implements the same
    interface via duck typing.

    If langchain-core is available, you may subclass BaseCallbackHandler:

        from langchain_core.callbacks import BaseCallbackHandler
        class MySentienceHandler(SentienceCallbackHandler, BaseCallbackHandler): ...

    Concurrency
    -----------
    As of v0.3.0.2 **one handler instance may be shared across overlapping
    invocations.**  All mutable execution state — session, builder, intent
    baseline and LLM turn telemetry — is keyed by root invocation rather
    than held on the instance, so two roots running at once on threads or
    on one event loop cannot exchange sessions, token usage, model,
    provider or ``llm_turn_id``.

    This holds for the callback surface, which carries ``run_id`` and
    ``parent_run_id``.  :class:`SentienceMiddleware` has no callback ids;
    its concurrency story is unchanged and is documented on that class.
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

        # Root key -> that root's governance state.  The key is the root's
        # run_id, or _LEGACY_ROOT when the caller supplies no callback ids.
        self._roots: Dict[Any, _RootState] = {}
        # run_id -> parent_run_id, for the ancestry walk.  Pruned with its
        # root; there is deliberately no other eviction policy (design 0001
        # §4.6 — evicting a live root would reintroduce the defect class
        # this release removes).
        self._ancestry: Dict[Any, Any] = {}
        # RLock covers threads.  asyncio callbacks on one loop cannot
        # interleave mid-method, so the lock is uncontended there.
        self._lock = threading.RLock()

        # The turn scope used when no callback ids are supplied. Held
        # separately so it survives across on_chain_start boundaries the
        # same way instance state did before v0.3.0.2 — on_chain_start has
        # never reset pending LLM telemetry.
        self._legacy_turn = _TurnState()

    # ------------------------------------------------------------------
    # Legacy compatibility views
    # ------------------------------------------------------------------
    #
    # Before v0.3.0.2 pending LLM telemetry lived in four instance
    # attributes.  They are now views onto the legacy branch scope, so the
    # handler holds no mutable execution state of its own while callers
    # that drive the handler without callback ids keep observing exactly
    # what they observed before.

    def _legacy_scope(self) -> _TurnState:
        root = self._roots.get(_LEGACY_ROOT)
        if root is None:
            return self._legacy_turn
        return root.turn_scopes.setdefault(None, self._legacy_turn)

    @property
    def _pending_llm_usage(self) -> Dict[str, Optional[int]]:
        return self._legacy_scope().usage

    @_pending_llm_usage.setter
    def _pending_llm_usage(self, value: Dict[str, Optional[int]]) -> None:
        self._legacy_scope().usage = value

    @property
    def _pending_llm_model(self) -> Optional[str]:
        return self._legacy_scope().model

    @_pending_llm_model.setter
    def _pending_llm_model(self, value: Optional[str]) -> None:
        self._legacy_scope().model = value

    @property
    def _pending_llm_provider(self) -> Optional[str]:
        return self._legacy_scope().provider

    @_pending_llm_provider.setter
    def _pending_llm_provider(self, value: Optional[str]) -> None:
        self._legacy_scope().provider = value

    @property
    def _pending_llm_turn_id(self) -> Optional[str]:
        return self._legacy_scope().turn_id

    @_pending_llm_turn_id.setter
    def _pending_llm_turn_id(self, value: Optional[str]) -> None:
        self._legacy_scope().turn_id = value

    # ------------------------------------------------------------------
    # Root and branch resolution
    # ------------------------------------------------------------------

    def _resolve_root(
        self,
        run_id: Any,
        parent_run_id: Any,
    ) -> Optional[_RootState]:
        """Walk ``parent_run_id`` ancestry up to a known root.

        A one-hop check is not enough: tool events parent to the node, not
        to the root.  Returns ``None`` when nothing resolves — the caller
        must then no-op rather than attach to an arbitrary session.
        """
        if run_id is None and parent_run_id is None:
            return self._roots.get(_LEGACY_ROOT)
        node = parent_run_id if parent_run_id is not None else run_id
        hops = 0
        while node is not None and node not in self._roots and hops < _MAX_HOPS:
            node = self._ancestry.get(node)
            hops += 1
        if node is None:
            return None
        return self._roots.get(node)

    def _branch_key(self, root: _RootState, parent_run_id: Any) -> Any:
        """Resolve which branch scope a callback belongs to.

        Walks up from ``parent_run_id`` so a tool nested deeper than its
        node (a tool inside a sub-chain) still finds its branch.  Falling
        back to the ``None`` key preserves flat-chain and legacy behaviour
        exactly: one root, one branch-less scope.
        """
        node = parent_run_id
        hops = 0
        while (
            node is not None
            and node not in root.turn_scopes
            and hops < _MAX_HOPS
        ):
            node = self._ancestry.get(node)
            hops += 1
        if node is not None and node in root.turn_scopes:
            return node
        return None

    def _turn_for(
        self,
        root: _RootState,
        parent_run_id: Any,
    ) -> Optional[_TurnState]:
        """The branch turn state a tool event should read, if any."""
        return root.turn_scopes.get(self._branch_key(root, parent_run_id))

    # ------------------------------------------------------------------
    # LangChain callback surface
    # ------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Map to AGENT_REGISTERED; capture intent from inputs.

        Only a ROOT start (``parent_run_id is None``) opens a session.  A
        nested start records ancestry and nothing else — LangGraph fires
        one of these per node, and treating them as sessions is what
        produced the false POL-001 this release removes.
        """
        run_id = kwargs.get("run_id")
        parent_run_id = kwargs.get("parent_run_id")

        with self._lock:
            if run_id is not None:
                self._ancestry[run_id] = parent_run_id
            if parent_run_id is not None:
                return

            key = run_id if run_id is not None else _LEGACY_ROOT
            root = self._open_root(key)

            # Emit AGENT_REGISTERED
            event = root.builder.build_agent_registered(
                agent_version=self._agent_version,
                vendor_id=self._vendor_id,
                declared_capabilities=self._declared_capabilities,
                owner_claim=self._owner_claim,
            )
            if event:
                self._sink.write(event, root.session_id)

            # Attempt to capture intent from inputs
            stated_objective = self._extract_intent(inputs)
            self._emit_intent(root, stated_objective)

    def on_chain_end(self, outputs: Dict[str, Any], **kwargs: Any) -> None:
        """Tear down only the root that is ending.

        A nested end resolves to no root and is a no-op.  Before
        v0.3.0.2 it tore the session down while the outer graph was still
        running, leaving every later tool call ungoverned.
        """
        run_id = kwargs.get("run_id")
        key = run_id if run_id is not None else _LEGACY_ROOT

        with self._lock:
            root = self._roots.get(key)
            if root is None:
                return

            if not root.intent_emitted:
                self._emit_intent(root, None)
            self._sink.session_closed(root.session_id, self._agent_id)
            self._sm.session_end(root.session_id)
            self._cache.clear_session(root.session_id)

            del self._roots[key]
            self._prune_ancestry(key)

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: Any,
        **kwargs: Any,
    ) -> None:
        """v0.2.3 Track 2 — turn-boundary reset, scoped to this branch.

        Fires at the START of every LLM turn.  Resets that branch's
        pending state and allocates a fresh ``llm_turn_id`` which the
        branch's subsequent ``on_tool_start`` events attach to their
        emitted CONTEXT_SNAPSHOT.

        Another branch's turn is untouched — in this root or any other.
        Two parallel nodes of one graph really do have overlapping LLM
        turns, so a single pending slot would let one branch's turn id
        land on another branch's tool event.
        """
        run_id = kwargs.get("run_id")
        parent_run_id = kwargs.get("parent_run_id")

        with self._lock:
            turn = self._turn_slot_for_llm(run_id, parent_run_id, create=True)
            if turn is None:
                return
            turn.reset(uuid.uuid4().hex)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """v0.2.3 Track 2 — capture token usage for the branch that ended.

        Populates that branch's usage / model / provider so subsequent
        ``on_tool_start`` events in the same branch attach the same usage
        AND the same ``llm_turn_id``.

        Defensive: any exception inside extraction is swallowed and usage
        falls back to all-None.  Token extraction must never break the
        agent's primary work.

        Note: the turn id is NOT regenerated here — it was allocated by
        ``on_llm_start`` and must persist across every event produced from
        this turn.  Writing usage into a *branch* scope rather than one
        per-root slot is exactly what stops a later-finishing branch from
        inheriting an earlier branch's turn id.
        """
        run_id = kwargs.get("run_id")
        parent_run_id = kwargs.get("parent_run_id")

        with self._lock:
            turn = self._turn_slot_for_llm(run_id, parent_run_id, create=True)
            if turn is None:
                return

            try:
                turn.usage = extract_from_langchain_response(response)
            except Exception:
                logger.debug(
                    "extract_from_langchain_response raised; falling back to all-None",
                    exc_info=True,
                )
                turn.usage = {field: None for field in CANONICAL_TOKEN_FIELDS}

            # Defensive: model / provider extraction also exception-safe.
            # A response object that explodes on getattr (custom __getattr__,
            # mocking gone wrong) must not break the handler.
            try:
                turn.model = self._extract_model_from_response(response)
            except Exception:
                logger.debug(
                    "_extract_model_from_response raised; falling back to None",
                    exc_info=True,
                )
                turn.model = None
            try:
                turn.provider = self._extract_provider_from_response(response)
            except Exception:
                logger.debug(
                    "_extract_provider_from_response raised; falling back to None",
                    exc_info=True,
                )
                turn.provider = None

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Emit INTENT_DECLARED (if not yet), SCOPE_ASSERTED, CONTEXT_SNAPSHOT."""
        parent_run_id = kwargs.get("parent_run_id")
        with self._lock:
            root = self._resolve_root(kwargs.get("run_id"), parent_run_id)
            if root is None:
                # No resolvable root.  Never invent a session: silently
                # attaching to an arbitrary one would produce exactly the
                # false attribution this release exists to remove.
                return
            self._emit_tool_start(
                root, serialized, input_str, self._turn_for(root, parent_run_id)
            )

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        """Update CONTEXT_SNAPSHOT with tool output (classification unknown)."""
        parent_run_id = kwargs.get("parent_run_id")
        with self._lock:
            root = self._resolve_root(kwargs.get("run_id"), parent_run_id)
            if root is None:
                return
            self._emit_tool_end(
                root, output, self._turn_for(root, parent_run_id)
            )

    # ------------------------------------------------------------------
    # Root lifecycle
    # ------------------------------------------------------------------

    def _open_root(self, key: Any) -> _RootState:
        """Create a session and governance state for one root invocation.

        The ``_LEGACY_ROOT`` entry is replaced unconditionally, which is
        precisely today's behaviour for callers that supply no callback
        ids.
        """
        session_id = str(uuid.uuid4())
        # v0.2.5: load operator-authored governance profile if present.
        # See mcp.py._start for full rationale.
        profile = GovernanceProfile.from_default_path_or_none()
        self._sm.session_start(
            session_id=session_id,
            agent_id=self._agent_id,
            profile=profile,
            # One handler legitimately governs overlapping root
            # invocations, so a second root for this agent is not a
            # collision. Without this, opening root B would force-close
            # root A's session while A is still running.
            allow_concurrent=True,
        )
        self._cache.init_session(session_id)
        builder = EventBuilder(
            session_manager=self._sm,
            cache=self._cache,
            agent_id=self._agent_id,
            session_id=session_id,
            deployment_mode=self._deployment_mode,
        )
        root = _RootState(session_id=session_id, builder=builder)
        if key is _LEGACY_ROOT:
            # Pending LLM telemetry has never been reset by a chain start;
            # carry the legacy scope across so it still is not.
            root.turn_scopes[None] = self._legacy_turn
        self._roots[key] = root
        return root

    def _prune_ancestry(self, key: Any) -> None:
        """Drop the ancestry subtree of a root that has just torn down."""
        if key is _LEGACY_ROOT:
            return
        doomed = []
        for node in self._ancestry:
            cursor = node
            hops = 0
            while cursor is not None and hops < _MAX_HOPS:
                if cursor == key:
                    doomed.append(node)
                    break
                if cursor in self._roots:
                    break
                cursor = self._ancestry.get(cursor)
                hops += 1
        for node in doomed:
            self._ancestry.pop(node, None)
        self._ancestry.pop(key, None)

    def _turn_slot_for_llm(
        self,
        run_id: Any,
        parent_run_id: Any,
        create: bool,
    ) -> Optional[_TurnState]:
        """The branch turn state an LLM callback should write.

        An LLM turn and the tool calls of the same branch are siblings —
        they share the node's ``run_id`` as ``parent_run_id`` — so the
        branch, not the LLM run, is the scope key.
        """
        if run_id is None and parent_run_id is None:
            # Legacy path.  Works even before any chain has started, which
            # is how the handler behaved before v0.3.0.2.
            return self._legacy_scope()

        root = self._resolve_root(run_id, parent_run_id)
        if root is None:
            return None
        key = self._branch_key(root, parent_run_id)
        if key is None and parent_run_id is not None and create:
            # First LLM event of this branch: open its scope.
            key = parent_run_id
        turn = root.turn_scopes.get(key)
        if turn is None:
            if not create:
                return None
            turn = _TurnState()
            root.turn_scopes[key] = turn
        return turn

    # ------------------------------------------------------------------
    # Emission, always against a resolved root
    # ------------------------------------------------------------------

    def _emit_tool_start(
        self,
        root: _RootState,
        serialized: Dict[str, Any],
        input_str: str,
        turn: Optional[_TurnState],
    ) -> None:
        if not root.intent_emitted:
            self._emit_intent(root, None)

        tool_name = serialized.get("name", "unknown_tool")
        operation_type = self._infer_operation_type(tool_name, input_str)

        # SCOPE_ASSERTED
        scope_event = root.builder.build_scope_asserted(
            tool_id=tool_name,
            asserted_permissions=[operation_type.value.lower()],
            target_system=self._extract_target_system(tool_name),
            operation_type=operation_type,
        )
        if scope_event:
            self._sink.write(scope_event, root.session_id)

        # CONTEXT_SNAPSHOT — at tool call with incoming data.
        # Attaches v0.2.3 Track 2 token-usage fields from this BRANCH's
        # most recent LLM turn (populated by on_llm_end). When on_llm_end
        # has not yet run for that turn (rare ordering — see plan §3.2
        # immutability rule), all token fields are None and only the
        # turn id (if on_llm_start ran) is attached. Already-emitted
        # events are NEVER mutated retroactively.
        ctx_event = root.builder.build_context_snapshot(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=[tool_name],
            retention_flags=[],
            context_size_tokens=len(input_str.split()) * 2,
            **self._token_kwargs(turn),
        )
        if ctx_event:
            self._sink.write(ctx_event, root.session_id)

    def _emit_tool_end(
        self,
        root: _RootState,
        output: str,
        turn: Optional[_TurnState],
    ) -> None:
        ctx_event = root.builder.build_context_snapshot(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=["tool_output"],
            retention_flags=[],
            context_size_tokens=len(str(output).split()) * 2,
            # Same turn id + usage — every event in the turn carries
            # the same attribution. Consumers dedupe by
            # (session_id, llm_turn_id) before summing token fields.
            **self._token_kwargs(turn),
        )
        if ctx_event:
            self._sink.write(ctx_event, root.session_id)

    # ------------------------------------------------------------------
    # v0.2.3 Track 2 — token-attribution helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _token_kwargs(turn: Optional[_TurnState]) -> Dict[str, Any]:
        """Build keyword args for build_context_snapshot from a turn scope.

        Takes the resolved branch turn state explicitly: it must never
        reach for handler state or guess which branch it belongs to.

        Returns the 5 numeric token fields + model + provider + turn id.
        Any field set to None on the result is dropped from the payload
        by the central serializer (per plan §1.3); no per-call filtering
        needed here.
        """
        if turn is None:
            turn = _TurnState()
        return {
            **turn.usage,
            "model_identifier": turn.model,
            "provider": turn.provider,
            "llm_turn_id": turn.turn_id,
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
    # Middleware binding (see SentienceMiddleware)
    # ------------------------------------------------------------------

    def _bind_middleware_root(self) -> Any:
        """Resolve the root a middleware-generated tool event belongs to.

        The middleware carries no callback ids, so binding is by active-root
        count rather than by ancestry:

          exactly one root -> that root (the supported single-run case)
          zero roots       -> the legacy entry, which may not exist
          two or more      -> _MIDDLEWARE_AMBIGUOUS; do not guess
        """
        with self._lock:
            active = [k for k in self._roots if k is not _LEGACY_ROOT]
            if len(active) > 1:
                return _MIDDLEWARE_AMBIGUOUS
            if len(active) == 1:
                return self._roots[active[0]]
            return self._roots.get(_LEGACY_ROOT)

    def _emit_middleware_ambiguity_error(self, tool_name: str) -> None:
        """Report that a middleware tool event could not be attributed.

        Emitted instead of a governance event, never alongside one.  The
        tool call itself still proceeds — governance is observe-only.
        """
        payload = GovernanceErrorPayload(
            error_type=ErrorType.INTERCEPT_FAILURE,
            severity=Severity.warning,
            failure_reason=(
                "SentienceMiddleware tool call could not be attributed: "
                f"{len([k for k in self._roots if k is not _LEGACY_ROOT])} "
                f"root invocations are open and the middleware carries no "
                f"run_id (tool={tool_name!r}). No governance event emitted."
            ),
            agent_continued=True,
        )
        event = GovernanceEvent(
            event_id=str(uuid.uuid4()),
            event_type=EventType.GOVERNANCE_ERROR,
            session_id="",
            event_sequence_number=0,
            previous_event_id=None,
            agent_id=self._agent_id,
            deployment_mode=self._deployment_mode,
            timestamp_utc=_now_utc(),
            primitive=PrimitiveType.SYSTEM,
            payload=payload,
            advisory_flags=[],
            policy_violations=[],
            simulated_consequence=None,
            pass_through=True,
        )
        self._sink.write(event, "")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit_intent(
        self,
        root: _RootState,
        stated_objective: Optional[str],
        source: IntentSource = IntentSource.inferred,
    ) -> None:
        """Emit the INTENT_DECLARED event for one root's session.

        Parameters
        ----------
        root
            The resolved root whose session receives the baseline. The
            early return tests ``root.intent_emitted``, not instance
            state — before v0.3.0.2 an instance flag survived the session
            reset, so a newly created session could never receive a
            baseline and its first tool call fired a false POL-001.
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
        if root.intent_emitted:
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

        event = root.builder.build_intent_declared(
            stated_objective=stated_objective,
            intent_source=source,
            intent_confidence=confidence,
            authorization_claim=self._owner_claim,
            session_scope_hint=self._declared_capabilities,
        )
        if event:
            self._sink.write(event, root.session_id)
        root.intent_emitted = True

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


def _now_utc() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


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

    Concurrency (unchanged in v0.3.0.2)
    -----------------------------------
    LangChain hands this middleware no ``run_id``, so a middleware tool
    event carries no ancestry to route on.  Binding is therefore by
    active-root count and one middleware instance per agent run remains
    the supported shape; per-step state below has the same assumption.
    Making the middleware concurrency-safe is a separate redesign.
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
        LangGraph step, that state is passed to the handler for THIS
        CALL ONLY — so the emitted CONTEXT_SNAPSHOT carries the
        step-level aggregated tokens + step turn id.  If no step state
        is set (handler used standalone, or step middleware not
        registered), the bound root's own branch state is used unchanged.

        Step state is passed as an argument rather than written onto the
        handler, so a step turn id cannot outlive its step by
        construction: there is nothing to restore afterwards.
        """
        serialized = {"name": tool_name}
        input_str = str(tool_input)

        binding = self._handler._bind_middleware_root()

        if binding is _MIDDLEWARE_AMBIGUOUS:
            # Two or more roots are open and nothing in the middleware
            # call identifies which one. Attributing to an arbitrary root
            # is the defect class this release removes, so report and
            # emit no governance event. The tool still runs.
            self._handler._emit_middleware_ambiguity_error(tool_name)
            return await next_call(tool_input)

        root = binding
        turn = self._step_turn_state()
        if root is not None and turn is None:
            turn = self._handler._turn_for(root, None)

        if root is not None:
            self._handler._emit_tool_start(root, serialized, input_str, turn)
        try:
            result = await next_call(tool_input)
        except Exception:
            # Fail-open: governance failure must never block agent
            raise
        if root is not None:
            self._handler._emit_tool_end(root, str(result), turn)
        return result

    def _step_turn_state(self) -> Optional[_TurnState]:
        """This step's aggregated telemetry, or None when no step is active.

        Usage is normalised to the canonical field set. ``awrap_step``
        already produces exactly those keys, but step state is a public
        attribute an integrator can set directly, and an unrecognised key
        reaching the event builder would raise inside the tool path.
        Governance must never break the agent's primary work.
        """
        if self._current_step_turn_id is None:
            return None
        supplied = self._current_step_usage or {}
        return _TurnState(
            usage={
                field: supplied.get(field) for field in CANONICAL_TOKEN_FIELDS
            },
            model=self._current_step_model,
            provider=self._current_step_provider,
            turn_id=self._current_step_turn_id,
        )

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
