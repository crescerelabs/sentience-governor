"""The Pydantic AI capability: session lifecycle and declaration.

Opens exactly one Sentience Governor session per Pydantic AI run, records
what the run says it is for, and closes the session when the run ends, on
the success path and the error path alike.

**Tool classification and token capture are not here yet.** Those arrive in
later checkpoints.
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
        """Visible fail-open for a malformed declaration (D1).

        Never silent, never raising, never blocking. The developer gets a
        warning at the keyboard, and a GOVERNANCE_ERROR goes through the
        shipped core evidence path.

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
        if self._rejection is None:
            return

        warnings.warn(
            f"Sentience Governor ignored this run's declaration: "
            f"{self._rejection}.",
            UserWarning,
            stacklevel=3,
        )
        self._emit(
            self._builder.build_governance_error(
                error_type=ErrorType.SCHEMA_VIOLATION,
                severity=Severity.warning,
                failure_reason=(
                    f"run declaration rejected: {self._rejection}"
                ),
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

