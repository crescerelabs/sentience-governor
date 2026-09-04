"""The Pydantic AI capability: session lifecycle (CP2).

Opens exactly one Sentience Governor session per Pydantic AI run and closes
it when the run ends, on the success path and the error path alike.

**This checkpoint carries no declaration reading, no classification, and no
token capture.** Those arrive in later checkpoints. What is here is the
session contract everything else will hang from.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic_ai.capabilities import AbstractCapability

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.session_manager.manager import SessionManager

_DEFAULT_AGENT_ID = "pydantic-ai-agent"


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

    def __init__(self, *, agent_id: str = _DEFAULT_AGENT_ID) -> None:
        self._agent_id = agent_id

        # Built once on the constructor instance and shared with every
        # per-run copy. The registry and cache are process-wide by design:
        # concurrent runs are isolated by session id, not by owning
        # separate managers.
        self._session_manager = SessionManager()
        self._cache = InProcessCache()

        # No sink is constructed here. `EventBuilder` takes none, and this
        # checkpoint emits nothing, so a sink would exist only to choose a
        # destination for events that do not yet exist. It arrives with the
        # first checkpoint that actually emits.

        # Per-run state. Populated in `wrap_run`, never on the constructor
        # instance, and never keyed per tool call.
        self._session_id: Optional[str] = None
        self._builder: Optional[EventBuilder] = None

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

        # 4. MANDATORY, and the one step with no failure signal.
        # `InProcessCache.set_intent_baseline` returns without effect when
        # the session has no cache entry, so omitting this raises nothing
        # and instead attaches SCOPE_INTENT_MISMATCH and POL-001 to every
        # later assertion, including plainly in-scope ones. It reads as a
        # policy result rather than a wiring bug. Every shipped adapter
        # does this immediately after session_start.
        self._cache.init_session(self._session_id)

    def _close_session(self) -> None:
        # 6. Both halves. Ending the session without clearing the cache
        # would leak per-session state for the life of the process.
        if self._session_id is None:
            return
        self._session_manager.session_end(self._session_id)
        self._cache.clear_session(self._session_id)

