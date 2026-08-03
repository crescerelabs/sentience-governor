"""MCP Path A — MCPClientLike abstraction, wrap_mcp_client, classification hook.

EXPLICIT CONSTRAINTS (from spec)
---------------------------------
* Do NOT assume any SDK method names, async model, or streaming model.
* Do NOT implement any concrete SDK binding.
* Only the abstraction and wrapper boundary are implemented here.
* SentienceMCPAdapter is the translation layer between a concrete SDK
  and the MCPClientLike Protocol.

MCPClientLike Protocol
----------------------
Sentience's internal contract.  Defines the minimal surface the Governance
Wrapper needs to intercept a tool call.  Not tied to any specific MCP SDK.

    class MCPClientLike(Protocol):
        def send_tool_call(self, tool_name: str, arguments: dict) -> dict: ...

Event emission semantics
------------------------
The wrapper emits governance events at the following points during a
single tool call lifecycle:

    1. SCOPE_ASSERTED        (before call, unchanged)
    2. (tool invoked)
    3. classification_hook   (after call, advisory, fail-open)
    4. CONTEXT_SNAPSHOT      (after call, using response data + hint)
    5. MEMORY_WRITE_ATTEMPT  (after call, if persistence, using args + hint)

Failure path (MUST-level):
    If the tool invocation raises:
    - SCOPE_ASSERTED has already been emitted
    - CONTEXT_SNAPSHOT MUST NOT be emitted
    - MEMORY_WRITE_ATTEMPT MUST NOT be emitted
    - classification_hook MUST NOT be invoked
    - The exception MUST propagate to the caller unchanged

This is a semantic correction from earlier versions of the wrapper,
which emitted CONTEXT_SNAPSHOT before the tool call using the argument
shape. Per the Golden Trace Package, CONTEXT_SNAPSHOT represents
"data now in the agent's context", which is the tool response — not
the request.

Classification hook
-------------------
The optional classification_hook parameter lets a caller inject
metadata from tool responses into the emitted CONTEXT_SNAPSHOT and
MEMORY_WRITE_ATTEMPT events. The hook is advisory and fail-open:

- Hook returning None or an all-None ClassificationHint         → defaults
- Hook returning a populated ClassificationHint                  → enrichment
- Hook raising any exception                                     → defaults + warning log

The hook CANNOT suppress events, modify tool results, or block
execution. See ClassificationHint and ClassificationHook below.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile import GovernanceProfile
from sentience_governor.schema.events import (
    ClassificationSource,
    DeploymentMode,
    DetectionMechanism,
    IntentConfidence,
    IntentSource,
    OperationType,
    WriteType,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import SinkWriter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Classification hook — public types
# ---------------------------------------------------------------------------

@dataclass
class ClassificationHint:
    """Advisory metadata a caller can inject after a tool call returns.

    Every field is Optional and defaults to None. Two cases that look
    similar but have different meanings:

    - Field set to None           -> use the wrapper's default for that field
    - Field set to [] or a value  -> caller's intentional value, use as-is

    This distinction is load-bearing. A caller who wants to explicitly
    declare "no classifications apply to this tool response" must pass
    ``data_classifications=[]``. A caller who wants the wrapper's default
    (currently also ``[]``, but conceptually different) passes None or
    omits the field.

    The wrapper implements this semantic via the ``_pick`` helper:
    ``value if value is not None else default``. An implicit falsy check
    (``value or default``) would collapse ``None`` and ``[]`` into the
    same branch, silently losing the distinction.
    """

    # Context snapshot fields
    data_classifications: Optional[List[str]] = None
    classification_source: Optional[ClassificationSource] = None
    provenance: Optional[List[str]] = None
    retention_flags: Optional[List[str]] = None
    context_size_tokens: Optional[int] = None

    # Memory write fields
    write_classification: Optional[str] = None
    retention_requested: Optional[str] = None

    # Approach A — LLM token accounting (v0.2.3 Track 2). All optional;
    # when present they ride on CONTEXT_SNAPSHOT event payloads alongside
    # the context fields above. None semantics: a field set to None means
    # "no value reported"; 0 is a real measurement and is preserved.
    # Cache fields preserve the provider's raw reporting — Sentience does
    # NOT recompute or normalise across providers (Anthropic excludes
    # cache reads from input_tokens, OpenAI includes them; the canonical
    # fields below mirror what the provider reported, no math applied).
    llm_prompt_tokens: Optional[int] = None
    llm_completion_tokens: Optional[int] = None
    llm_cached_read_tokens: Optional[int] = None
    llm_cached_write_tokens: Optional[int] = None
    llm_reasoning_tokens: Optional[int] = None
    model_identifier: Optional[str] = None
    provider: Optional[str] = None
    llm_turn_id: Optional[str] = None


ClassificationHook = Callable[[str, dict, Any], Optional[ClassificationHint]]
"""Post-call metadata-injection hook.

Signature: ``(tool_name, arguments, result) -> Optional[ClassificationHint]``

The ``result`` parameter is typed ``Any``, not ``dict``. Tool results can
be any JSON-compatible value (string, list, number, dict, None). The hook
is responsible for handling whatever shape it receives. Hooks that assume
``result`` is a dict must defend against that assumption being wrong.

The hook is called exactly once per successful tool call, and is NOT
called when the underlying tool raises. See the module docstring for the
full lifecycle contract.
"""


def _pick(value: Any, *, default: Any) -> Any:
    """Return ``value`` if it is not None, otherwise ``default``.

    Explicit implementation of the None-vs-intentional-empty semantic
    for ClassificationHint fields. Must be used in place of ``value or
    default`` (which collapses None and empty collections).
    """
    return value if value is not None else default


# ---------------------------------------------------------------------------
# Internal MCP adapter boundary (normative — spec §APIs)
# ---------------------------------------------------------------------------

class MCPClientLike:
    """Minimal internal abstraction for intercepting MCP-style tool calls.

    This is NOT the canonical MCP ecosystem interface.  It is Sentience's
    internal contract — the only surface wrap_mcp_client() targets.

    Concrete MCP SDK adapters implement this interface via SentienceMCPAdapter.
    No SDK method names, async model, or streaming model are assumed here.
    """

    def send_tool_call(self, tool_name: str, arguments: dict) -> dict:
        """Send a tool call and return its result.

        Implementors: adapt your SDK's concrete call surface to this method.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SentienceMCPAdapter — translation layer (SDK → MCPClientLike)
# ---------------------------------------------------------------------------

class SentienceMCPAdapter(MCPClientLike):
    """Wraps any object that exposes a tool-call surface, adapting it to
    MCPClientLike.  No concrete SDK is assumed.

    The caller supplies a ``delegate`` object and a ``call_fn`` that knows
    how to invoke a tool call on that delegate.  This keeps the adapter
    free of any SDK-specific method names.

    Parameters
    ----------
    delegate : Any
        The underlying SDK client or session object.
    call_fn : callable
        ``call_fn(delegate, tool_name, arguments) -> dict``
        Adapts the delegate's native call interface to our contract.
    """

    def __init__(self, delegate: Any, call_fn: Any) -> None:
        self._delegate = delegate
        self._call_fn = call_fn

    def send_tool_call(self, tool_name: str, arguments: dict) -> dict:
        return self._call_fn(self._delegate, tool_name, arguments)


# ---------------------------------------------------------------------------
# Governance proxy returned by wrap_mcp_client()
# ---------------------------------------------------------------------------

class _GovernedMCPProxy(MCPClientLike):
    """Governance-instrumented proxy around an MCPClientLike target.

    Emits governance events at the control points for every tool call.
    See module docstring for the full emission sequence and failure
    path contract.
    """

    # Known persistence target tool-type keywords (default fallback list v0)
    _PERSISTENCE_KEYWORDS = {"database", "vector_store", "filesystem", "logging"}

    def __init__(
        self,
        target: MCPClientLike,
        builder: EventBuilder,
        sink_writer: SinkWriter,
        session_id: str,
        agent_id: str,
        authorization_claim: Optional[str],
        classification_hook: Optional[ClassificationHook] = None,
    ) -> None:
        self._target = target
        self._builder = builder
        self._sink = sink_writer
        self._session_id = session_id
        self._agent_id = agent_id
        self._authorization_claim = authorization_claim
        self._classification_hook = classification_hook

    def send_tool_call(self, tool_name: str, arguments: dict) -> Any:
        """Intercept, emit governance events, invoke tool, return result.

        Event sequence (success path):
            SCOPE_ASSERTED  -> invoke tool  -> classification_hook
            -> CONTEXT_SNAPSHOT  -> MEMORY_WRITE_ATTEMPT (if persistence)
            -> return result

        Event sequence (tool raises):
            SCOPE_ASSERTED  -> invoke tool  -> exception propagates
            -> NO CONTEXT_SNAPSHOT, NO MEMORY_WRITE_ATTEMPT, NO hook call

        Return type is ``Any`` because tool results are not constrained
        to dicts; any JSON-compatible value (string, list, number, None)
        is legal.
        """
        operation_type = self._infer_operation_type(tool_name, arguments)
        target_system = tool_name.split(".")[0] if "." in tool_name else tool_name

        # 1. SCOPE_ASSERTED (before call)
        scope_event = self._builder.build_scope_asserted(
            tool_id=tool_name,
            asserted_permissions=[operation_type.value.lower()],
            target_system=target_system,
            operation_type=operation_type,
            authorization_claim=self._authorization_claim,
        )
        if scope_event:
            self._sink.write(scope_event, self._session_id)

        # 2. Invoke the real tool. Fail-open: on exception, nothing else
        # fires and the exception propagates to the caller unchanged.
        try:
            result = self._target.send_tool_call(tool_name, arguments)
        except Exception as exc:
            logger.error("Tool call failed: %s — agent continues", exc)
            raise

        # 3. Call classification hook (post-call, fail-open).
        hint = self._invoke_hook_safely(tool_name, arguments, result)

        # 4. CONTEXT_SNAPSHOT (post-call, hint-enriched or defaulted).
        # Every field uses _pick() to implement the explicit
        # None-vs-intentional-empty semantic documented on ClassificationHint.
        ctx_event = self._builder.build_context_snapshot(
            data_classifications=_pick(
                hint.data_classifications if hint else None,
                default=[],
            ),
            classification_source=_pick(
                hint.classification_source if hint else None,
                default=ClassificationSource.unclassified,
            ),
            provenance=_pick(
                hint.provenance if hint else None,
                default=[target_system],
            ),
            retention_flags=_pick(
                hint.retention_flags if hint else None,
                default=[],
            ),
            # NOTE: default is _estimate_tokens(result), NOT arguments.
            # CONTEXT_SNAPSHOT represents data now in the agent's
            # context after the tool returned.
            context_size_tokens=_pick(
                hint.context_size_tokens if hint else None,
                default=self._estimate_tokens(result),
            ),
            authorization_claim=self._authorization_claim,
            # v0.2.3 Track 2 — LLM token accounting. Pass-through only:
            # the wrapper does not extract these from the tool call
            # itself (it can't — it sees tool calls, not LLM responses).
            # Adopters who want token tracking populate these fields on
            # the ClassificationHint they return from the hook. None
            # values are omitted by the payload's serializer (per
            # plan §1.3); zero values are preserved.
            llm_prompt_tokens=hint.llm_prompt_tokens if hint else None,
            llm_completion_tokens=hint.llm_completion_tokens if hint else None,
            llm_cached_read_tokens=hint.llm_cached_read_tokens if hint else None,
            llm_cached_write_tokens=hint.llm_cached_write_tokens if hint else None,
            llm_reasoning_tokens=hint.llm_reasoning_tokens if hint else None,
            model_identifier=hint.model_identifier if hint else None,
            provider=hint.provider if hint else None,
            llm_turn_id=hint.llm_turn_id if hint else None,
        )
        if ctx_event:
            self._sink.write(ctx_event, self._session_id)

        # 5. MEMORY_WRITE_ATTEMPT (post-call, if persistence target).
        if self._is_persistence_target(tool_name, arguments):
            mem_event = self._builder.build_memory_write_attempt(
                write_type=WriteType.write_to_persistence_target,
                detection_mechanism=DetectionMechanism.tool_metadata,
                target_store=target_system,
                write_classification=_pick(
                    hint.write_classification if hint else None,
                    default="unclassified",
                ),
                # NOTE: write_size_tokens always derives from arguments
                # (the write payload), never from the response. A write
                # event's "size" is what was persisted, not what was
                # observed afterwards.
                write_size_tokens=self._estimate_tokens(arguments),
                retention_requested=_pick(
                    hint.retention_requested if hint else None,
                    default=None,
                ),
            )
            if mem_event:
                self._sink.write(mem_event, self._session_id)

        return result

    def _invoke_hook_safely(
        self, tool_name: str, arguments: dict, result: Any
    ) -> Optional[ClassificationHint]:
        """Call the configured classification hook, swallowing any exception.

        Returns
        -------
        ClassificationHint on success.
        None if no hook is configured, the hook returned None, or the
        hook raised an exception.

        Logging: bounded. On hook exception, emits a single WARNING log
        line containing the tool name, exception class name, and
        exception message. The tool result itself is NEVER logged — it
        may contain sensitive data. The arguments are also NEVER logged
        — they may contain credentials or PII.
        """
        if self._classification_hook is None:
            return None
        try:
            return self._classification_hook(tool_name, arguments, result)
        except Exception as exc:
            logger.warning(
                "classification_hook raised for tool=%s: %s: %s; using defaults",
                tool_name,
                exc.__class__.__name__,
                exc,
            )
            return None

    @staticmethod
    def _infer_operation_type(tool_name: str, arguments: dict) -> OperationType:
        name_lower = tool_name.lower()
        if any(k in name_lower for k in ("write", "update", "create", "insert", "put")):
            return OperationType.WRITE
        if any(k in name_lower for k in ("delete", "remove", "drop")):
            return OperationType.DELETE
        if any(k in name_lower for k in ("exec", "run", "execute")):
            return OperationType.EXECUTE
        return OperationType.READ

    def _is_persistence_target(self, tool_name: str, arguments: dict) -> bool:
        name_lower = tool_name.lower()
        return any(kw in name_lower for kw in self._PERSISTENCE_KEYWORDS)

    @staticmethod
    def _estimate_tokens(data: Any) -> int:
        import json as _json
        try:
            return max(1, len(_json.dumps(data)) // 4)
        except Exception:
            return 1


# ---------------------------------------------------------------------------
# Public API: wrap_mcp_client()
# ---------------------------------------------------------------------------

def wrap_mcp_client(
    target: MCPClientLike,
    session_manager: SessionManager,
    cache: InProcessCache,
    sink_writer: SinkWriter,
    agent_id: str,
    deployment_mode: DeploymentMode = DeploymentMode.vendor_managed,
    agent_version: Optional[str] = None,
    vendor_id: Optional[str] = None,
    declared_capabilities: Optional[List[str]] = None,
    owner_claim: Optional[str] = None,
    stated_objective: Optional[str] = None,
    session_id: Optional[str] = None,
    classification_hook: Optional[ClassificationHook] = None,
) -> "_WrappedMCPSession":
    """Return a governance-wrapped proxy targeting the MCPClientLike boundary.

    The returned object exposes session_start() as an async context manager
    and supports explicit session_start() / session_end() calls.

    Parameters
    ----------
    classification_hook
        Optional advisory hook called after each successful tool call to
        inject classification metadata into the CONTEXT_SNAPSHOT and
        MEMORY_WRITE_ATTEMPT events the wrapper emits. See the module
        docstring and the ClassificationHook type for the full contract.
        Defaults to None (no hook, all metadata defaulted).
    """
    return _WrappedMCPSession(
        target=target,
        session_manager=session_manager,
        cache=cache,
        sink_writer=sink_writer,
        agent_id=agent_id,
        deployment_mode=deployment_mode,
        agent_version=agent_version,
        vendor_id=vendor_id,
        declared_capabilities=declared_capabilities or [],
        owner_claim=owner_claim,
        stated_objective=stated_objective,
        session_id=session_id,
        classification_hook=classification_hook,
    )


class _WrappedMCPSession:
    """Session-scoped governance wrapper.  Use as async context manager or
    call session_start() / session_end() explicitly.
    """

    def __init__(
        self,
        target: MCPClientLike,
        session_manager: SessionManager,
        cache: InProcessCache,
        sink_writer: SinkWriter,
        agent_id: str,
        deployment_mode: DeploymentMode,
        agent_version: Optional[str],
        vendor_id: Optional[str],
        declared_capabilities: List[str],
        owner_claim: Optional[str],
        stated_objective: Optional[str],
        session_id: Optional[str],
        classification_hook: Optional[ClassificationHook] = None,
    ) -> None:
        self._target = target
        self._sm = session_manager
        self._cache = cache
        self._sink = sink_writer
        self._agent_id = agent_id
        self._deployment_mode = deployment_mode
        self._agent_version = agent_version
        self._vendor_id = vendor_id
        self._declared_capabilities = declared_capabilities
        self._owner_claim = owner_claim
        self._stated_objective = stated_objective
        self._session_id = session_id or str(uuid.uuid4())
        self._classification_hook = classification_hook
        self._builder: Optional[EventBuilder] = None
        self._proxy: Optional[_GovernedMCPProxy] = None

    # ------------------------------------------------------------------
    # Async context manager (primary usage)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "_WrappedMCPSession":
        self._start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        self._end()

    # ------------------------------------------------------------------
    # Explicit start / end
    # ------------------------------------------------------------------

    def _start(self) -> None:
        # v0.2.5: load operator-authored governance profile if present.
        # When no file exists, profile is None and the session runs on
        # the v0.2.4 code path (no profile transforms, no profile
        # metadata in trace).
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

        # AGENT_REGISTERED
        reg_event = self._builder.build_agent_registered(
            agent_version=self._agent_version,
            vendor_id=self._vendor_id,
            declared_capabilities=self._declared_capabilities,
            owner_claim=self._owner_claim,
        )
        if reg_event:
            self._sink.write(reg_event, self._session_id)

        # INTENT_DECLARED (immediately if stated_objective provided)
        if self._stated_objective:
            intent_event = self._builder.build_intent_declared(
                stated_objective=self._stated_objective,
                intent_source=IntentSource.explicit,
                intent_confidence=IntentConfidence.explicit,
                authorization_claim=self._owner_claim,
                session_scope_hint=self._declared_capabilities,
            )
        else:
            intent_event = self._builder.build_intent_declared(
                stated_objective=None,
                intent_source=IntentSource.none,
                intent_confidence=IntentConfidence.unknown,
                authorization_claim=None,
                session_scope_hint=[],
            )
        if intent_event:
            self._sink.write(intent_event, self._session_id)

        self._proxy = _GovernedMCPProxy(
            target=self._target,
            builder=self._builder,
            sink_writer=self._sink,
            session_id=self._session_id,
            agent_id=self._agent_id,
            authorization_claim=self._owner_claim,
            classification_hook=self._classification_hook,
        )

    def _end(self) -> None:
        if self._session_id:
            self._sink.session_closed(self._session_id, self._agent_id)
            self._sm.session_end(self._session_id)
            self._cache.clear_session(self._session_id)

    # ------------------------------------------------------------------
    # Proxy the tool-call surface
    # ------------------------------------------------------------------

    def send_tool_call(self, tool_name: str, arguments: dict) -> Any:
        if self._proxy is None:
            raise RuntimeError("Session not started.  Use as context manager.")
        return self._proxy.send_tool_call(tool_name, arguments)
