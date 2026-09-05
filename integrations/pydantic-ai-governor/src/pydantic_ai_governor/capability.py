"""The Pydantic AI capability: session lifecycle and declaration.

Opens exactly one Sentience Governor session per Pydantic AI run, records
what the run says it is for, and closes the session when the run ends, on
the success path and the error path alike.

It also holds the execution boundary — assert, dispatch, snapshot — and
records each model turn's measured token usage.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Optional

from pydantic_ai.capabilities import AbstractCapability

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.schema.events import (
    ErrorType,
    IntentConfidence,
    IntentSource,
    Severity,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import FileSink, SinkWriter

from pydantic_ai_governor import evidence
from pydantic_ai_governor.declaration import Declaration, resolve

_DEFAULT_AGENT_ID = "pydantic-ai-agent"

# ---------------------------------------------------------------------------
# Trace destination — decided here, deliberately, not inherited.
#
# This is the first checkpoint that writes anything, so it is the first that
# has to say where. The choice: `~/.sentience/traces/pydantic-ai/`, one file
# per session named for the session id.
#
# Why this and not something else. Sentience Governor already keeps agent
# traces under `~/.sentience/traces/<harness>/` with a file per session
# (`wrapper/claude_code_hook.py:136`). An operator running `sentience pulse`
# or the CLI viewer expects to find traces in one place regardless of which
# agent produced them, and a second location would fragment that for no gain.
# The convention is adopted because it is right for the operator, not merely
# because another adapter uses it.
#
# It is NOT configurable. No constructor option and no environment variable:
# neither is authorized, and a configurable destination is a contract that
# should be added on evidence of need rather than in advance.
# ---------------------------------------------------------------------------
_TRACE_ROOT = (".sentience", "traces", "pydantic-ai")


def _trace_file(session_id: str) -> Path:
    """This session's trace file, creating the directory if needed.

    Resolved per call rather than at import, so the location follows the
    process's actual home.
    """
    directory = Path.home().joinpath(*_TRACE_ROOT)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{session_id}.jsonl"


class SentienceGovernor(AbstractCapability[Any]):
    """Agent Execution Evidence for a Pydantic AI agent.

    Attach it to an agent and each run opens its own governed session::

        agent = Agent(model, capabilities=[SentienceGovernor()])

    One Pydantic AI run is one Sentience Governor session. The run's own
    ``run_id`` is the session id, so the two systems agree on identity
    without a mapping table. When the run ends, the session ends.

    A resumed run (after a deferred tool call, for example) is a new
    Pydantic run with a new ``run_id``, and therefore a new session. That
    is deliberate: this capability introduces no cross-run correlation.
    """

    def __init__(
        self,
        *,
        objective: Optional[str] = None,
        scope: Optional[Any] = None,
        agent_id: str = _DEFAULT_AGENT_ID,
    ) -> None:
        self._agent_id = agent_id
        # Constructor values are DEFAULTS. A per-run metadata block
        # overrides them; see `for_run`.
        self._default = Declaration(
            objective=objective, scope=tuple(scope or ())
        )

        # Built once on the constructor instance and shared with every
        # per-run copy. The registry and cache are process-wide by design:
        # concurrent runs are isolated by session id, not by owning
        # separate managers.
        self._session_manager = SessionManager()
        self._cache = InProcessCache()

        # Per-run state. Populated in `for_run` / `wrap_run`, never on the
        # constructor instance, and never keyed per tool call.
        self._session_id: Optional[str] = None
        self._builder: Optional[EventBuilder] = None
        self._sink: Optional[SinkWriter] = None
        self._declaration: Declaration = self._default
        self._rejection: Optional[str] = None
        # Most recent resolved classification, for the checkpoint that will
        # emit evidence from it. Never keyed per call on `self`.
        self._classification = evidence.Classification()

    # -- Pydantic AI listing identity -------------------------------------
    @staticmethod
    def get_serialization_name() -> str | None:
        return "sentience-governor"

    # -- per-run isolation -------------------------------------------------
    async def for_run(self, ctx: Any) -> "SentienceGovernor":
        """Return a fresh instance for this run.

        Async because that is the hook's shape at pydantic-ai 2.37.0. All
        per-run state is derived from ``ctx`` inside ``wrap_run`` rather
        than captured here, which is what the hook's own durability
        contract asks for: a worker re-deriving this instance from a
        deserialized context must reach the same place.
        """
        # `type(self)`, never a hardcoded class: a subclass must survive
        # `for_run`, or its overrides are silently dropped for the run and
        # the capability appears to do nothing.
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone._session_id = None
        clone._builder = None
        clone._sink = None
        # Derived from `ctx`, which is what the hook's durability contract
        # requires: a worker re-deriving this instance from a deserialized
        # context must reach the same declaration.
        clone._declaration, clone._rejection = resolve(
            getattr(ctx, "metadata", None), self._default
        )
        return clone

    # -- session lifecycle -------------------------------------------------
    async def wrap_run(self, ctx: Any, *, handler: Any) -> Any:
        """Open a session for this run, and close it whatever happens.

        The six steps below are a contract, not an implementation detail.
        Step 4 in particular has no failure signal of its own: see
        ``_open_session``.

        ``handler`` takes no arguments at pydantic-ai 2.37.0.
        """
        self._open_session(ctx)
        try:
            return await handler()
        finally:
            # Teardown belongs in `finally`, not after the await: a run
            # that raises must not leave a session open, and the shipped
            # LangChain adapter's inactivity reaper is a backstop rather
            # than a plan.
            self._close_session()

    # -- execution boundary ------------------------------------------------
    async def wrap_tool_execute(
        self, ctx: Any, *, call: Any, tool_def: Any, args: Any, handler: Any
    ) -> Any:
        """The execution boundary: assert, dispatch, snapshot.

        Ordering is the evidence. `SCOPE_ASSERTED` fires **after validation
        and immediately before dispatch**, so it records a call that really
        is about to run: Pydantic guarantees a validation-failed call never
        reaches this hook, which is why nothing here has to detect one. The
        snapshot fires only on a normal return, so a raised tool leaves an
        assertion with no snapshot and the exception propagates untouched.

        That asymmetry is the outcome signal this schema has. The absence
        of a snapshot is positional evidence that the call did not return;
        there is no execution-outcome field to set, and none is invented.

        The handler takes the validated args dict at pydantic-ai 2.37.0.
        """
        classification, rejection = evidence.resolve(
            getattr(tool_def, "metadata", None)
        )
        self._classification = classification
        if rejection is not None:
            # D2: a configuration-contract failure, not a policy decision.
            # It says the metadata did not parse and makes no claim about
            # the action itself.
            self._fail_open(
                f"Sentience Governor ignored the classification on tool "
                f"'{call.tool_name}': {rejection}.",
                f"tool classification rejected for '{call.tool_name}': "
                f"{rejection}",
            )

        tool_name = call.tool_name
        tool_use_id = getattr(call, "tool_call_id", None)

        # UNKNOWN maps to READ with no permissions purely so the event can
        # be serialized: core has no undeclared-operation semantic. That
        # READ is a compatibility fallback, NOT a claim the tool read
        # anything. See `evidence.to_core_operation`.
        operation_type, permissions = classification.core_operation()
        self._emit(
            self._builder.build_scope_asserted(
                tool_id=tool_name,
                asserted_permissions=permissions,
                target_system=classification.target_for(tool_name),
                operation_type=operation_type,
                tool_use_id=tool_use_id,
            )
        )

        # Nothing between the assertion and the dispatch. Anything that
        # could raise here would produce an assertion for a call that never
        # ran, which is the one shape this ordering exists to prevent.
        result = await handler(args)

        self._emit(
            self._builder.build_context_snapshot(
                data_classifications=list(classification.data_classifications),
                classification_source=classification.source,
                provenance=[classification.target_for(tool_name)],
                retention_flags=[],
                # An ESTIMATED context token count, using core's own
                # estimator so the field carries the meaning the product
                # already gives it. Measured model-token usage is a
                # separate concern on separate fields.
                context_size_tokens=evidence.estimate_context_tokens(result),
                tool_use_id=tool_use_id,
            )
        )
        return result

    # -- token and model evidence -----------------------------------------
    async def after_model_request(
        self, ctx: Any, *, request_context: Any, response: Any
    ) -> Any:
        """Record one model turn's measured usage, and change nothing.

        **The response is returned exactly as it arrived.** This hook is a
        reader: it does not touch parts, usage, identity or control flow,
        and an agent run behaves identically with the capability attached
        and without it.

        Everything recorded comes off `response`. **`ctx.usage` is not a
        source**, because at this hook it lags by one request: a delta
        taken from it credits each turn with the previous turn's tokens
        and never attributes the last one. See `evidence.read_turn`.

        Token snapshots carry no advisory flags and no violations — the
        builder deliberately does not run them through `_eval_context`,
        since a snapshot that observes no data classification would
        otherwise manufacture a POL-003 on every turn.
        """
        if self._builder is None:
            # No open session, so nothing to attribute this turn to.
            # Recording it against a session that does not exist would be
            # worse than not recording it.
            return response

        turn = evidence.read_turn(response, getattr(ctx, "run_step", None))
        self._emit(
            self._builder.build_token_snapshot(
                llm_turn_id=turn.llm_turn_id,
                # Measured, not the CP5 estimator: the provider reported
                # the actual input size for this turn.
                context_size_tokens=turn.context_size_tokens,
                llm_prompt_tokens=turn.llm_prompt_tokens,
                llm_completion_tokens=turn.llm_completion_tokens,
                llm_cached_read_tokens=turn.llm_cached_read_tokens,
                llm_cached_write_tokens=turn.llm_cached_write_tokens,
                model_identifier=turn.model_identifier,
                provider=turn.provider,
                # Core's own join. The ids this turn issued, in response
                # order, so an analyzer can attribute a tool call's
                # violation to the turn that paid for it.
                tool_use_ids=turn.tool_use_ids,
                # Says whether `llm_turn_id` is provider-issued or our
                # local fallback, positively in both directions.
                provenance=turn.provenance,
            )
        )
        return response

    # -- internals ---------------------------------------------------------
    def _open_session(self, ctx: Any) -> None:
        # 1. The Pydantic run id IS the Governor session id.
        self._session_id = ctx.run_id

        # 2. The builder is per-session and holds the session id.
        self._builder = EventBuilder(
            session_manager=self._session_manager,
            cache=self._cache,
            agent_id=self._agent_id,
            session_id=self._session_id,
        )

        # 3. `allow_concurrent=True` because sibling runs on one Agent share
        # an agent_id, and the default force-closes the sibling's session.
        self._session_manager.session_start(
            session_id=self._session_id,
            agent_id=self._agent_id,
            allow_concurrent=True,
        )

        # The sink: this is the first checkpoint that emits, so the first
        # that needs somewhere to write.
        self._sink = SinkWriter(FileSink(str(_trace_file(self._session_id))))

        # 4. MANDATORY, and the one step with no failure signal.
        # `InProcessCache.set_intent_baseline` returns without effect when
        # the session has no cache entry, so omitting this raises nothing
        # and instead attaches SCOPE_INTENT_MISMATCH and POL-001 to every
        # later assertion, including plainly in-scope ones. It reads as a
        # policy result rather than a wiring bug. Every shipped adapter
        # does this immediately after session_start.
        self._cache.init_session(self._session_id)

        # 5. Registration, then what this run says it is for.
        self._emit(
            self._builder.build_agent_registered(
                agent_version=None,
                vendor_id="pydantic-ai",
                declared_capabilities=list(self._declaration.scope),
                owner_claim=None,
            )
        )
        self._declare_intent()
        self._report_rejection()

    def _declare_intent(self) -> None:
        """INTENT_DECLARED, honest about where the objective came from.

        A declared objective is integrator-vouched at invocation time,
        which is stronger provenance than construction time, so it is
        `explicit` on both axes. With nothing declared from either source
        the event still fires, carrying `none` / `unknown` and no
        objective: a fabricated one would be worse than an absent one.
        """
        declared = self._declaration.is_declared
        self._emit(
            self._builder.build_intent_declared(
                stated_objective=self._declaration.objective if declared else None,
                intent_source=IntentSource.explicit if declared else IntentSource.none,
                intent_confidence=(
                    IntentConfidence.explicit if declared
                    else IntentConfidence.unknown
                ),
                authorization_claim=None,
                session_scope_hint=list(self._declaration.scope) if declared else [],
            )
        )

    def _report_rejection(self) -> None:
        """Visible fail-open for a malformed run declaration (D1)."""
        if self._rejection is None:
            return
        self._fail_open(
            f"Sentience Governor ignored this run's declaration: "
            f"{self._rejection}.",
            f"run declaration rejected: {self._rejection}",
        )

    def _fail_open(self, warning_text: str, failure_reason: str) -> None:
        """The shared visible-fail-open path for D1 and D2.

        Never silent, never raising, never blocking. The developer gets a
        warning at the keyboard, and a GOVERNANCE_ERROR goes through the
        shipped core evidence path. One implementation serves both
        decisions: a malformed declaration and a malformed classification
        are the same kind of failure, and they should not drift apart.

        **Where that error surfaces is core's decision, not ours.**
        `SinkWriter` short-circuits this event type to stdout regardless of
        the configured sink (`sink/writer.py:124-127`), so it is
        developer-visible and is not persisted to the session trace. The
        integration honors that rather than bypassing, duplicating or
        working around it: reimplementing sink routing is precisely what
        this package must not do.

        Neither the warning nor `failure_reason` carries a metadata value.
        Both leave this process, and what captures or retains them is
        outside our control, so the rule is absolute rather than
        conditional on a destination.
        """
        warnings.warn(warning_text, UserWarning, stacklevel=3)
        self._emit(
            self._builder.build_governance_error(
                error_type=ErrorType.SCHEMA_VIOLATION,
                severity=Severity.warning,
                failure_reason=failure_reason,
            )
        )

    def _emit(self, event: Any) -> None:
        """Write one event, if the builder produced one."""
        if event is not None and self._sink is not None and self._session_id:
            self._sink.write(event, self._session_id)

    def _close_session(self) -> None:
        # 6. Both halves. Ending the session without clearing the cache
        # would leak per-session state for the life of the process.
        if self._session_id is None:
            return
        self._session_manager.session_end(self._session_id)
        self._cache.clear_session(self._session_id)

