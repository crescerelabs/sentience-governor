"""SentienceCallbackHandler is usable as a plain LangChain callback.

The handler duck-types ``BaseCallbackHandler`` so that the core package has
no import-time dependency on ``langchain-core``, and the guide documents
passing it directly: ``agent.invoke(..., config={"callbacks": [handler]})``.

LangChain's callback manager reads more than the ``on_*`` methods from every
handler: ``ignore_<kind>`` before dispatching an event, ``raise_error`` when a
callback fails, and ``run_inline`` for async dispatch. Without them the manager
raises ``AttributeError`` out of the governed application's own ``invoke()``
before any governance event is written. These tests pin that contract.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.schema.events import GovernanceEvent
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import SinkWriter
from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler


class _CapturingSink:
    def __init__(self) -> None:
        self.events: List[GovernanceEvent] = []

    def write(self, event: GovernanceEvent) -> bool:
        self.events.append(event)
        return True


def _make_handler(cls: Any = SentienceCallbackHandler) -> tuple[_CapturingSink, Any]:
    captured = _CapturingSink()
    handler = cls(
        agent_id="test-langchain-agent",
        session_manager=SessionManager(),
        cache=InProcessCache(),
        sink_writer=SinkWriter(captured),
        agent_version="1.0.0",
        vendor_id="test-vendor",
        declared_capabilities=["crm_fetch"],
        owner_claim="user-test",
    )
    return captured, handler


# The flags LangChain's callback manager reads before dispatching each event
# kind (langchain_core.callbacks.manager.handle_event / _ahandle_event_for_handler).
_IGNORE_FLAGS = (
    "ignore_llm",
    "ignore_retry",
    "ignore_chain",
    "ignore_agent",
    "ignore_retriever",
    "ignore_chat_model",
    "ignore_custom_event",
)


@pytest.mark.parametrize("flag", _IGNORE_FLAGS)
def test_handler_exposes_every_ignore_flag_as_false(flag):
    _, handler = _make_handler()
    assert getattr(handler, flag) is False


def test_handler_exposes_raise_error_and_run_inline_as_false():
    """A failing governance callback must be logged by LangChain, never
    re-raised into the agent (raise_error=False), and dispatch stays on the
    default executor path (run_inline=False), matching BaseCallbackHandler."""
    _, handler = _make_handler()
    assert handler.raise_error is False
    assert handler.run_inline is False


def test_on_chat_model_start_defers_to_on_llm_start():
    """Like BaseCallbackHandler, raise NotImplementedError so LangChain falls
    back to on_llm_start, where the handler opens the LLM turn."""
    _, handler = _make_handler()
    with pytest.raises(NotImplementedError):
        handler.on_chat_model_start({}, [[]])


def test_plain_handler_governs_a_real_langchain_runnable():
    """End to end with a real langchain-core, no model API: a chain that calls
    a local tool, with the plain handler passed exactly as the guide shows."""
    pytest.importorskip("langchain_core")
    from langchain_core.runnables import RunnableConfig, RunnableLambda
    from langchain_core.tools import tool

    @tool
    def crm_fetch(account: str) -> str:
        """Fetch an account."""
        return f"account {account}"

    def body(inputs: dict, config: RunnableConfig) -> str:
        return crm_fetch.invoke({"account": "acme"}, config=config)

    captured, handler = _make_handler()
    out = RunnableLambda(body).invoke(
        {"input": "Look up acme"}, config={"callbacks": [handler]}
    )

    assert out == "account acme"
    kinds = [e.event_type.value for e in captured.events]
    assert kinds.count("AGENT_REGISTERED") == 1
    scope = [e for e in captured.events if e.event_type.value == "SCOPE_ASSERTED"]
    assert [e.payload.tool_id for e in scope] == ["crm_fetch"]
