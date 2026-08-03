"""v0.3.0 CP5: hook intent-baseline rehydration (capture-side).

Enforces the operator-approved acceptance criteria: a real intent declaration
appended to a session's trace (as `declare_intent` will do in CP6) becomes
visible to POL-001 evaluation in a *later* hook process (each hook invocation
builds a fresh cache), so post-declaration matching activity stops firing the
missing-intent POL-001 — while pre-declaration POL-001 events stay unchanged
(non-retroactive) and declaration-free sessions are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from sentience_governor.wrapper import claude_code_hook as cch
from sentience_governor.wrapper.claude_code_hook import (
    ClaudeCodeGovernanceHook,
    _first_declared_intent,
)

SESSION = "sess-rehydrate-1"


@pytest.fixture
def sink_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.jsonl"


@pytest.fixture(autouse=True)
def _default_profile(monkeypatch):
    """Deterministic default posture (demand_at=session_start), independent of
    the operator's real ~/.sentience/profile.yaml."""
    monkeypatch.setattr(
        cch.GovernanceProfile,
        "from_default_path_or_none",
        staticmethod(lambda: None),
    )


def _write_payload(use_id: str = "use-1", session: str = SESSION) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": "Write",  # (WRITE, "filesystem") — mutating
        "tool_input": {"file_path": "/tmp/x.txt", "content": "hi"},
        "tool_use_id": use_id,
        "cwd": "/tmp",
    }


def _run(payload: dict, sink: Path) -> None:
    ClaudeCodeGovernanceHook(payload, sink).process()


def _events(sink: Path) -> List[dict]:
    if not sink.exists():
        return []
    return [json.loads(l) for l in sink.read_text().splitlines() if l.strip()]


def _scope_events(sink: Path) -> List[dict]:
    return [e for e in _events(sink) if e["event_type"] == "SCOPE_ASSERTED"]


def _append_declaration(
    sink: Path,
    objective,
    scope,
    *,
    source: str = "inferred",
    session: str = SESSION,
) -> None:
    """Append a real INTENT_DECLARED, mimicking what declare_intent (CP6) will
    write to the trace out-of-band from the hook."""
    decl = {
        "event_type": "INTENT_DECLARED",
        "session_id": session,
        "event_id": "decl-1",
        "payload": {
            "intent_source": source,
            "stated_objective": objective,
            "session_scope_hint": scope,
        },
    }
    with sink.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(decl) + "\n")


class TestFirstDeclaredIntent:
    def test_declaration_free_trace_returns_none(self, sink_path):
        # Session start emits INTENT_DECLARED(intent_source=none) only.
        _run(_write_payload(), sink_path)
        assert _first_declared_intent(sink_path) is None

    def test_returns_first_real_declaration(self, sink_path):
        _run(_write_payload(), sink_path)
        _append_declaration(sink_path, "edit files", ["filesystem"])
        assert _first_declared_intent(sink_path) == ("edit files", ["filesystem"])

    def test_none_source_is_not_a_real_declaration(self, sink_path):
        _run(_write_payload(), sink_path)
        _append_declaration(sink_path, None, [], source="none")
        assert _first_declared_intent(sink_path) is None

    def test_first_write_wins_among_real_declarations(self, sink_path):
        _run(_write_payload(), sink_path)
        _append_declaration(sink_path, "first", ["filesystem"])
        _append_declaration(sink_path, "second", ["web"])
        assert _first_declared_intent(sink_path) == ("first", ["filesystem"])

    def test_missing_file_returns_none(self, tmp_path):
        assert _first_declared_intent(tmp_path / "nope.jsonl") is None


class TestRehydrationEndToEnd:
    def test_declaration_suppresses_later_pol_001_not_earlier(self, sink_path):
        # 1. Pre-declaration mutating write -> POL-001 fires.
        _run(_write_payload("use-1"), sink_path)
        pre = _scope_events(sink_path)
        assert len(pre) == 1
        assert "POL-001" in pre[0]["policy_violations"]

        # 2. A real declaration lands (matching scope), as declare_intent will.
        _append_declaration(sink_path, "edit project files", ["filesystem"])

        # 3. Post-declaration matching write -> POL-001 no longer fires.
        _run(_write_payload("use-2"), sink_path)
        post = _scope_events(sink_path)
        assert len(post) == 2
        assert "POL-001" not in post[1]["policy_violations"]
        # The scope-mismatch advisory is likewise gone for the matching target.
        assert "SCOPE_INTENT_MISMATCH" not in post[1]["advisory_flags"]

        # 4. Non-retroactive: the pre-declaration POL-001 event is unchanged.
        assert "POL-001" in _scope_events(sink_path)[0]["policy_violations"]

    def test_declaration_free_session_keeps_firing_pol_001(self, sink_path):
        # Rehydration sets no baseline without a real declaration -> every
        # mutating write keeps firing POL-001 (byte-identical behavior).
        _run(_write_payload("use-1"), sink_path)
        _run(_write_payload("use-2"), sink_path)
        scopes = _scope_events(sink_path)
        assert len(scopes) == 2
        assert all("POL-001" in s["policy_violations"] for s in scopes)

    def test_none_source_declaration_does_not_suppress(self, sink_path):
        _run(_write_payload("use-1"), sink_path)
        _append_declaration(sink_path, None, [], source="none")
        _run(_write_payload("use-2"), sink_path)
        scopes = _scope_events(sink_path)
        assert all("POL-001" in s["policy_violations"] for s in scopes)

    def test_out_of_scope_declaration_still_fires_pol_001(self, sink_path):
        # A declaration whose scope does NOT cover the target still trips the
        # SCOPE_INTENT_MISMATCH -> POL-001 (scope is load-bearing).
        _run(_write_payload("use-1"), sink_path)
        _append_declaration(sink_path, "browse web", ["web"])  # not filesystem
        _run(_write_payload("use-2"), sink_path)
        post = _scope_events(sink_path)[1]
        assert "POL-001" in post["policy_violations"]
        assert "SCOPE_INTENT_MISMATCH" in post["advisory_flags"]
