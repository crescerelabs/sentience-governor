"""v0.3.0 CP6: sentience_declare_intent — the one forward-looking write.

Exercises the full path end to end (CP3 identification + CP6 server write +
CP5 rehydration): a mid-session declaration suppresses POL-001 for SUBSEQUENT
matching activity while leaving pre-declaration POL-001 untouched
(non-retroactive), and fails closed on uncertain binding.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

import pytest

from sentience_governor.mcp_server.server import (
    declare_intent_payload,
    session_status_payload,
)
from sentience_governor.wrapper import claude_code_hook as cch

SESSION = "sess-cp6"


@pytest.fixture(autouse=True)
def _default_profile(monkeypatch):
    monkeypatch.setattr(
        cch.GovernanceProfile,
        "from_default_path_or_none",
        staticmethod(lambda: None),
    )


@pytest.fixture(autouse=True)
def _trace_dir(monkeypatch, tmp_path):
    """Point both the resolver and the writer at a temp trace dir."""
    from sentience_governor.cli import ux

    monkeypatch.setattr(ux, "_resolve_trace_dir", lambda: tmp_path)
    return tmp_path


def _events(sink: Path) -> List[dict]:
    if not sink.exists():
        return []
    return [json.loads(l) for l in sink.read_text().splitlines() if l.strip()]


def _scopes(sink: Path) -> List[dict]:
    return [e for e in _events(sink) if e["event_type"] == "SCOPE_ASSERTED"]


def _write(sink: Path, use_id: str, session: str = SESSION) -> None:
    cch.ClaudeCodeGovernanceHook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.txt", "content": "hi"},
            "tool_use_id": use_id,
            "cwd": "/tmp",
        },
        sink_path=sink,
    ).process()


class TestDeclareIntentValidation:
    def test_empty_objective_is_invalid(self, _trace_dir):
        out = declare_intent_payload("   ", ["filesystem"], env={})
        assert out["status"] == "invalid_request"
        assert out["written"] is False

    def test_empty_scope_is_invalid(self, _trace_dir):
        out = declare_intent_payload("edit files", [], env={})
        assert out["status"] == "invalid_request"
        assert out["written"] is False

    def test_scope_of_blank_strings_is_invalid(self, _trace_dir):
        out = declare_intent_payload("edit files", ["  "], env={})
        assert out["status"] == "invalid_request"


class TestDeclareIntentFailClosed:
    def test_no_live_session_writes_nothing(self, _trace_dir):
        # Empty trace dir + no env id -> cannot bind -> fail closed.
        out = declare_intent_payload("edit files", ["filesystem"], env={})
        assert out["status"] == "no_current_session"
        assert out["written"] is False
        # Nothing was created.
        assert list(_trace_dir.glob("*.jsonl")) == []

    def test_not_yet_captured_writes_nothing(self, _trace_dir):
        out = declare_intent_payload(
            "edit files", ["filesystem"],
            env={"CLAUDE_CODE_SESSION_ID": "ghost"},
        )
        assert out["status"] == "not_yet_captured"
        assert out["written"] is False


class TestDeclareIntentEndToEnd:
    def test_mid_session_declaration_is_forward_only(self, _trace_dir):
        sink = _trace_dir / f"{SESSION}.jsonl"

        # 1. Pre-declaration mutating write -> POL-001 fires (turn before N).
        _write(sink, "u1")
        assert "POL-001" in _scopes(sink)[0]["policy_violations"]

        # 2. Declare intent (matching scope) via the MCP tool payload.
        out = declare_intent_payload(
            "edit project files", ["filesystem"],
            env={"CLAUDE_CODE_SESSION_ID": SESSION},
        )
        assert out["status"] == "declared"
        assert out["written"] is True
        assert out["session_id"] == SESSION
        assert out["event_id"]
        assert out["intent_source"] == "inferred"
        assert out["intent_confidence"] == "inferred_low"
        assert out["session_scope_hint"] == ["filesystem"]

        # The declaration is a real INTENT_DECLARED on the trace, append-only.
        declared = [
            e for e in _events(sink)
            if e["event_type"] == "INTENT_DECLARED"
            and e["payload"].get("intent_source") != "none"
        ]
        assert len(declared) == 1
        assert declared[0]["payload"]["stated_objective"] == "edit project files"

        # 3. Post-declaration matching write -> POL-001 no longer fires.
        _write(sink, "u2")
        scopes = _scopes(sink)
        assert len(scopes) == 2
        assert "POL-001" not in scopes[1]["policy_violations"]

        # 4. Non-retroactive: turn before N keeps its POL-001.
        assert "POL-001" in scopes[0]["policy_violations"]

    def test_declaration_out_of_scope_still_flags(self, _trace_dir):
        sink = _trace_dir / f"{SESSION}.jsonl"
        _write(sink, "u1")
        out = declare_intent_payload(
            "browse the web", ["web"],  # does NOT cover filesystem writes
            env={"CLAUDE_CODE_SESSION_ID": SESSION},
        )
        assert out["status"] == "declared"
        _write(sink, "u2")
        # Scope mismatch keeps POL-001 firing (scope is load-bearing).
        assert "POL-001" in _scopes(sink)[1]["policy_violations"]


class TestDeclareIntentWriteFreshnessGate:
    """declare_intent uses the TIGHT write freshness window (safety gate): a
    stale env candidate that a read/status would still accept must fail closed
    for the write, so a declaration is never misattributed to a prior session."""

    def test_stale_within_read_window_fails_closed_for_write(self, _trace_dir):
        sink = _trace_dir / f"{SESSION}.jsonl"
        _write(sink, "u1")  # a real, freshly-appended trace
        # Simulate 300s of elapsed time: inside the 1800s read window, outside
        # the 90s write window. Advancing the resolver's clock (not the file
        # mtime) keeps this deterministic.
        now = os.path.getmtime(sink) + 300

        out = declare_intent_payload(
            "edit files", ["filesystem"],
            env={"CLAUDE_CODE_SESSION_ID": SESSION}, now=now,
        )
        assert out["status"] == "no_current_session"
        assert out["written"] is False
        # Nothing was appended: no real (non-none) INTENT_DECLARED on the trace.
        real = [
            e for e in _events(sink)
            if e["event_type"] == "INTENT_DECLARED"
            and e["payload"].get("intent_source") != "none"
        ]
        assert real == []

    def test_same_stale_trace_still_resolves_for_read_status(self, _trace_dir):
        sink = _trace_dir / f"{SESSION}.jsonl"
        _write(sink, "u1")
        now = os.path.getmtime(sink) + 300  # inside read window, outside write

        status = session_status_payload(
            env={"CLAUDE_CODE_SESSION_ID": SESSION}, now=now,
        )
        # The looser read window tolerates the idle trace: status still reads.
        assert status["status"] == "current_session"
        assert status["session_id"] == SESSION

    def test_fresh_trace_within_write_window_declares(self, _trace_dir):
        sink = _trace_dir / f"{SESSION}.jsonl"
        _write(sink, "u1")
        now = os.path.getmtime(sink) + 30  # within the 90s write window

        out = declare_intent_payload(
            "edit files", ["filesystem"],
            env={"CLAUDE_CODE_SESSION_ID": SESSION}, now=now,
        )
        assert out["status"] == "declared"
        assert out["written"] is True


# A fixed clock so file mtimes and the resolver's `now` are deterministic.
_NOW = 2_000_000_000.0


def _trace_at(trace_dir, sid, age_seconds):
    """Create a trace file whose mtime is _NOW - age_seconds."""
    path = trace_dir / f"{sid}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    mtime = _NOW - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def _no_real_declaration(trace_dir):
    """True iff no trace under trace_dir gained a non-none INTENT_DECLARED."""
    for p in trace_dir.glob("*.jsonl"):
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if (
                e.get("event_type") == "INTENT_DECLARED"
                and (e.get("payload") or {}).get("intent_source") not in (None, "none")
            ):
                return False
    return True


class TestDeclareIntentFailClosedBeltAndSuspenders:
    """CP6-level fail-closed coverage (belt-and-suspenders over the CP3
    resolver tests): declare_intent must write nothing when the binding is
    stale, ambiguous, or conflicting."""

    def test_stale_env_candidate_fails_closed(self, _trace_dir):
        # Env names a captured session, but its trace is far outside the 90s
        # write window (idle/old): a declaration must not bind to it.
        _trace_at(_trace_dir, SESSION, age_seconds=600)
        out = declare_intent_payload(
            "edit files", ["filesystem"],
            env={"CLAUDE_CODE_SESSION_ID": SESSION}, now=_NOW,
        )
        assert out["status"] == "no_current_session"
        assert out["written"] is False
        assert _no_real_declaration(_trace_dir)

    def test_ambiguous_fresh_traces_fail_closed(self, _trace_dir):
        # No env id and two equally-fresh traces: which is live is ambiguous.
        _trace_at(_trace_dir, "sess-A", age_seconds=20)
        _trace_at(_trace_dir, "sess-B", age_seconds=30)
        out = declare_intent_payload("edit files", ["filesystem"], env={}, now=_NOW)
        assert out["status"] == "no_current_session"
        assert out["written"] is False
        assert _no_real_declaration(_trace_dir)

    def test_env_conflicts_with_fresher_active_trace_fails_closed(self, _trace_dir):
        # Env candidate is fresh, but a DIFFERENT trace is fresher and active:
        # the env is possibly stale (server reuse), so fail closed.
        _trace_at(_trace_dir, "sess-A", age_seconds=60)  # candidate, fresh
        _trace_at(_trace_dir, "sess-B", age_seconds=5)   # fresher, active
        out = declare_intent_payload(
            "edit files", ["filesystem"],
            env={"CLAUDE_CODE_SESSION_ID": "sess-A"}, now=_NOW,
        )
        assert out["status"] == "no_current_session"
        assert out["written"] is False
        assert "conflict" in out["detail"]
        assert _no_real_declaration(_trace_dir)
