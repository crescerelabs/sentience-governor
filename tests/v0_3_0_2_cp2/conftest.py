"""CP2 shared fixtures. Mirrors tests/test_langchain_handler.py::_make_handler
so the new suite constructs the handler exactly as the existing 32 do."""
import uuid
import pytest

from sentience_governor.session_manager import SessionManager
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.sink.writer import SinkWriter
from sentience_governor.wrapper.langchain_adapter import (
    SentienceCallbackHandler,
    SentienceMiddleware,
)


class CapturingSink:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)
        return True


def as_dict(event):
    return event.to_dict() if hasattr(event, "to_dict") else event


def violations(events):
    out = []
    for e in events:
        out.extend(str(v) for v in (as_dict(e).get("policy_violations") or []))
    return out


def pol001(events):
    return [v for v in violations(events) if "POL-001" in v or "POL_001" in v]


def sessions(events):
    return [as_dict(e).get("session_id") for e in events]


def events_for(events, session_id):
    return [e for e in events if as_dict(e).get("session_id") == session_id]


def turn_ids(events):
    return [as_dict(e).get("llm_turn_id") for e in events
            if as_dict(e).get("llm_turn_id") is not None]


def make_handler(declared=("test.read",)):
    sink = CapturingSink()
    handler = SentienceCallbackHandler(
        agent_id="cp2-agent",
        session_manager=SessionManager(),
        cache=InProcessCache(),
        sink_writer=SinkWriter(sink),
        agent_version="1.0.0",
        vendor_id="cp2",
        declared_capabilities=list(declared),
        owner_claim="user-cp2",
    )
    return sink, handler


def rid():
    return uuid.uuid4()


@pytest.fixture
def handler_pair():
    return make_handler()
