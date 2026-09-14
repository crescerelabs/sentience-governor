"""v0.3.2 CP2: sticky per-session policy across Claude Code hook processes.

Each hook invocation is a fresh process; these tests simulate that by
constructing one ``ClaudeCodeGovernanceHook`` per event against the same
``sink_base``. Row numbers reference the v0.3.2 sticky-state design's
expected-output table (checkpoint CP1-D) and the release plan's test rows:
8-15 end to end, 12a/12b crash points, 40/51 shared snapshots,
50/52 public evidence, and row 15 for the MCP and LangChain adapters, which
resolve once per session and never touch the binding machinery.

The registration provenance fields (``profile_resolution`` /
``profile_binding`` on AGENT_REGISTERED) are a CP3 deliverable; here the
registration carries the fingerprint only and the provenance-aware
agreement rule is exercised with synthetic registrations in
tests/test_session_binding.py.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List, Optional

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.profile import GovernanceProfile
from sentience_governor.profile import loader as loader_module
from sentience_governor.schema.events import EventType, GovernanceEvent
from sentience_governor.session_manager import resumption as res
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.session_manager.resumption import (
    read_session_binding,
    sidecar_path_for,
)
from sentience_governor.sink.writer import SinkWriter
from sentience_governor.wrapper import claude_code_hook as cch
from sentience_governor.wrapper.claude_code_hook import ClaudeCodeGovernanceHook

HEX64 = re.compile(r"[0-9a-f]{64}")
HEX12 = re.compile(r"^[0-9a-f]{12}$")

PROFILE_A = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  demand_at: first_write\n"
    "task_boundary:\n"
    "  signals: [dir_change, time_gap]\n"
    "  time_gap_seconds: 120\n"
    "high_consequence:\n"
    "  tools:\n"
    "    - \"Bash:shell\"\n"
)
PROFILE_B = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  required: true\n"
    "  demand_at: first_tool_call\n"
)
PROFILE_D = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  required: true\n"
    "  demand_at: session_start\n"
    "task_boundary:\n"
    "  signals: [time_gap]\n"
)

S1 = "sess-sticky-0001-aaaa"
S2 = "sess-sticky-0002-bbbb"


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, home: Path, sink_base: Path):
        self.home = home
        self.sink_base = sink_base
        self.profiles = home / "profiles"
        self.resolution = home / "resolution.yaml"
        self.default = home / "profile.yaml"

    def write_profile(self, name: str, text: str) -> None:
        (self.profiles / name).write_text(text, encoding="utf-8")

    def write_resolution(self, *bindings: tuple) -> None:
        lines = ["schema_version: 1", "bindings:"]
        for pattern, target in bindings:
            lines += [f"  - agent_id: {pattern}", f"    profile: {target}"]
        self.resolution.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def fingerprint_of(self, name: str) -> str:
        return GovernanceProfile.from_file(self.profiles / name).fingerprint()

    def sink_for(self, session_id: str, shared: bool = False) -> Path:
        return cch._session_file_for(self.sink_base, shared, session_id)


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> Env:
    home = tmp_path / "home"
    (home / "profiles").mkdir(parents=True)
    e = Env(home, tmp_path / "traces")
    e.write_profile("A.yaml", PROFILE_A)
    e.write_profile("B.yaml", PROFILE_B)
    e.write_resolution(("claude-code-*", "profiles/A.yaml"))
    monkeypatch.setattr(loader_module, "DEFAULT_RESOLUTION_PATH", e.resolution)
    monkeypatch.setattr(loader_module, "DEFAULT_PROFILE_PATH", e.default)
    monkeypatch.delenv("SENTIENCE_CLAUDE_CODE_AGENT_ID_PREFIX", raising=False)
    return e


# ---------------------------------------------------------------------------
# hook process simulation
# ---------------------------------------------------------------------------


def _pre(session: str, use_id: str = "use-1", tool: str = "Write") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": {"file_path": "/tmp/x.txt", "content": "hi"},
        "tool_use_id": use_id,
        "cwd": "/tmp",
    }


def _post(session: str, use_id: str = "use-1", tool: str = "Write") -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": {"file_path": "/tmp/x.txt", "content": "hi"},
        "tool_response": {"ok": True},
        "tool_use_id": use_id,
        "cwd": "/tmp",
    }


def _end(session: str, transcript: Optional[Path] = None) -> dict:
    p = {"hook_event_name": "SessionEnd", "session_id": session, "cwd": "/tmp"}
    if transcript is not None:
        p["transcript_path"] = str(transcript)
    return p


def _run(env: Env, payload: dict, *, shared: bool = False) -> None:
    ClaudeCodeGovernanceHook(payload, sink_base=env.sink_base, shared_file_mode=shared).process()


def _declare(env: Env, session: str, *, shared: bool = False) -> Optional[str]:
    hook = ClaudeCodeGovernanceHook({}, sink_base=env.sink_base, shared_file_mode=shared)
    return hook.emit_intent_declaration(session, "ship the thing", ["filesystem"])


def _events(sink: Path, session: Optional[str] = None) -> List[dict]:
    if not sink.exists():
        return []
    out = [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines() if l.strip()]
    if session is not None:
        out = [e for e in out if e["session_id"] == session]
    return out


def _fingerprints(events: List[dict]) -> List[Optional[str]]:
    return [e.get("profile_fingerprint") for e in events]


def _registrations(events: List[dict]) -> List[dict]:
    return [e for e in events if e["event_type"] == EventType.AGENT_REGISTERED.value]


def _warnings(caplog) -> List[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------------------------------------------------------------------
# rows 8-11: the sticky contract
# ---------------------------------------------------------------------------


class TestStickySession:
    def test_row8_config_switch_mid_session_does_not_move_the_session(self, env: Env, caplog):
        fa = env.fingerprint_of("A.yaml")
        fb = env.fingerprint_of("B.yaml")
        assert fa != fb
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _run(env, _pre(S1, "use-1"))
            env.write_resolution(("claude-code-*", "profiles/B.yaml"))
            _run(env, _post(S1, "use-1"))
            _run(env, _pre(S1, "use-2"))
            assert _declare(env, S1) is not None
            _run(env, _post(S1, "use-2"))
        sink = env.sink_for(S1)
        events = _events(sink)
        assert len(_registrations(events)) == 1
        assert set(_fingerprints(events)) == {fa}
        entry = read_session_binding(sink, S1)
        assert entry["profile_fingerprint"] == fa
        assert entry["resolution"] == "bound" and entry["binding"] == "claude-code-*"
        assert entry["recovered_from"] is None
        assert entry["bound_by"].startswith("claude_code_hook/")
        assert not _warnings(caplog)
        # A NEW session now resolves to B.
        _run(env, _pre(S2))
        assert set(_fingerprints(_events(env.sink_for(S2)))) == {fb}

    def test_row9_profile_file_edited_mid_session(self, env: Env):
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        snap = sink.parent / read_session_binding(sink, S1)["snapshot"]
        bytes_before = snap.read_bytes()
        env.write_profile("A.yaml", PROFILE_B)
        _run(env, _post(S1, "use-1"))
        _run(env, _pre(S1, "use-2"))
        assert set(_fingerprints(_events(sink))) == {fa}
        assert snap.read_bytes() == bytes_before
        # The next session picks up the edit.
        _run(env, _pre(S2))
        assert set(_fingerprints(_events(env.sink_for(S2)))) == {env.fingerprint_of("A.yaml")}

    def test_row10_two_concurrent_sessions_keep_their_own_profiles(self, env: Env):
        fa = env.fingerprint_of("A.yaml")
        fb = env.fingerprint_of("B.yaml")
        env.write_resolution(
            ("claude-code-sess-sti", "profiles/A.yaml"),  # both sessions share this prefix
        )
        # Bind S1 to A, then switch the file so S2 binds to B, interleave.
        _run(env, _pre(S1, "use-1"))
        env.write_resolution(("claude-code-*", "profiles/B.yaml"))
        _run(env, _pre(S2, "use-1"))
        env.write_profile("A.yaml", PROFILE_D)
        env.write_profile("B.yaml", PROFILE_D)
        _run(env, _post(S1, "use-1"))
        _run(env, _post(S2, "use-1"))
        _run(env, _pre(S1, "use-2"))
        _run(env, _pre(S2, "use-2"))
        assert set(_fingerprints(_events(env.sink_for(S1)))) == {fa}
        assert set(_fingerprints(_events(env.sink_for(S2)))) == {fb}
        e1 = read_session_binding(env.sink_for(S1), S1)
        e2 = read_session_binding(env.sink_for(S2), S2)
        assert e1["profile_fingerprint"] == fa and e2["profile_fingerprint"] == fb
        snaps = sorted((env.sink_base / "profiles").glob("*.json"))
        assert len(snaps) == 2

    def test_row11_session_end_then_resume_rehydrates(self, env: Env, tmp_path: Path):
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1, "use-1"))
        _run(env, _post(S1, "use-1"))
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                        "content": [{"type": "tool_use", "id": "use-1", "name": "Write", "input": {}}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        _run(env, _end(S1, transcript))
        sink = env.sink_for(S1)
        before = len(_events(sink))
        assert before >= 3
        entry = read_session_binding(sink, S1)
        assert entry is not None and entry["recovered_from"] is None
        _run(env, _pre(S1, "use-9"))
        events = _events(sink)
        assert len(events) > before
        assert len(_registrations(events)) == 1
        assert set(_fingerprints(events)) == {fa}
        assert read_session_binding(sink, S1) == entry


# ---------------------------------------------------------------------------
# crash matrix (rows 12a, 12b) and recovery through real processes (12c-12e)
# ---------------------------------------------------------------------------


class TestCrashMatrix:
    def test_row12a_crash_after_binding_before_registration(self, env: Env):
        fa = env.fingerprint_of("A.yaml")
        sink = env.sink_for(S1)
        # Establish the binding exactly as the first process would, then "crash".
        with res.sink_lock(sink):
            cch._establish_or_rehydrate_binding(sink, S1, cch._derive_agent_id(S1), None)
        assert not sink.exists() or sink.stat().st_size == 0
        entry = read_session_binding(sink, S1)
        assert entry is not None
        env.write_resolution(("claude-code-*", "profiles/B.yaml"))  # config moved meanwhile
        _run(env, _pre(S1))
        events = _events(sink)
        assert len(_registrations(events)) == 1
        assert set(_fingerprints(events)) == {fa}
        assert read_session_binding(sink, S1)["recovered_from"] is None

    def test_row12b_crash_after_registration_before_offsets(self, env: Env):
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        sidecar = sidecar_path_for(sink)
        index = json.loads(sidecar.read_text())
        del index[S1]  # offsets lost, binding bucket kept
        sidecar.write_text(json.dumps(index))
        _run(env, _post(S1, "use-1"))
        events = _events(sink)
        assert len(_registrations(events)) == 1
        assert [e["event_sequence_number"] for e in events] == list(range(1, len(events) + 1))
        assert set(_fingerprints(events)) == {fa}
        assert read_session_binding(sink, S1)["recovered_from"] is None

    def test_row12c_sidecar_deleted_unchanged_config_is_silent(self, env: Env, caplog):
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        sidecar_path_for(sink).unlink()
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _run(env, _post(S1, "use-1"))
        events = _events(sink)
        assert len(_registrations(events)) == 1
        assert set(_fingerprints(events)) == {fa}
        assert read_session_binding(sink, S1)["recovered_from"] == "rematerialized"
        assert not _warnings(caplog)

    def test_row12d_sidecar_deleted_changed_profile_reresolves_visibly(self, env: Env, caplog):
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        sidecar_path_for(sink).unlink()
        env.write_profile("A.yaml", PROFILE_B)
        fa_prime = env.fingerprint_of("A.yaml")
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _run(env, _post(S1, "use-1"))
            _run(env, _pre(S1, "use-2"))
        events = _events(sink)
        assert _registrations(events)[0]["profile_fingerprint"] == fa
        assert _fingerprints(events)[-1] == fa_prime
        assert read_session_binding(sink, S1)["recovered_from"] == "reresolve"
        warnings = _warnings(caplog)
        assert len(warnings) == 1, warnings  # once, not per later process
        assert S1[:12] in warnings[0] and fa in warnings[0] and fa_prime in warnings[0]

    def test_row12e_snapshot_deleted_is_rematerialized_before_rebinding(self, env: Env):
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        snap = sink.parent / read_session_binding(sink, S1)["snapshot"]
        snap.unlink()
        _run(env, _post(S1, "use-1"))
        assert snap.is_file() and res.content_hash_of(snap.read_bytes()) == snap.stem
        assert read_session_binding(sink, S1)["recovered_from"] == "rematerialized"
        assert set(_fingerprints(_events(sink))) == {fa}


# ---------------------------------------------------------------------------
# rows 13, 14: degraded and shared-file mode
# ---------------------------------------------------------------------------


class TestModes:
    def test_row13_degraded_stays_degraded_even_after_the_target_is_fixed(self, env: Env):
        env.default.write_text(PROFILE_D, encoding="utf-8")
        fd = GovernanceProfile.from_file(env.default).fingerprint()
        env.write_resolution(("claude-code-*", "profiles/missing.yaml"))
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        entry = read_session_binding(sink, S1)
        assert entry["resolution"] == "degraded"
        assert entry["binding"] == "claude-code-*"
        assert entry["profile_fingerprint"] == fd
        env.write_profile("missing.yaml", PROFILE_A)  # fixed mid-session
        _run(env, _post(S1, "use-1"))
        _run(env, _pre(S1, "use-2"))
        assert set(_fingerprints(_events(sink))) == {fd}
        assert read_session_binding(sink, S1) == entry
        # A new session picks up the fixed binding.
        _run(env, _pre(S2))
        assert read_session_binding(env.sink_for(S2), S2)["resolution"] == "bound"

    def test_row14_shared_file_mode_one_sidecar_two_entries(self, env: Env):
        fa = env.fingerprint_of("A.yaml")
        fb = env.fingerprint_of("B.yaml")
        env.sink_base = env.sink_base / "shared.jsonl"
        _run(env, _pre(S1, "use-1"), shared=True)
        env.write_resolution(("claude-code-*", "profiles/B.yaml"))
        _run(env, _pre(S2, "use-1"), shared=True)
        _run(env, _post(S1, "use-1"), shared=True)  # update_session_state for S1
        _run(env, _post(S2, "use-1"), shared=True)
        sink = env.sink_base
        assert sink.is_file()
        index = json.loads(sidecar_path_for(sink).read_text())
        bucket = index[res._SESSION_BINDING_KEY]
        assert set(bucket) == {S1, S2}
        assert bucket[S1]["profile_fingerprint"] == fa
        assert bucket[S2]["profile_fingerprint"] == fb
        assert set(_fingerprints(_events(sink, S1))) == {fa}
        assert set(_fingerprints(_events(sink, S2))) == {fb}
        assert (sink.parent / "profiles").is_dir()
        assert len(list((sink.parent / "profiles").glob("*.json"))) == 2


# ---------------------------------------------------------------------------
# rows 40/51, 50/52: shared snapshots and public evidence
# ---------------------------------------------------------------------------


class TestEvidence:
    def test_rows40_51_two_sessions_identical_content_share_one_snapshot(self, env: Env):
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1))
        _run(env, _pre(S2))
        e1 = read_session_binding(env.sink_for(S1), S1)
        e2 = read_session_binding(env.sink_for(S2), S2)
        assert e1["snapshot"] == e2["snapshot"]
        assert len(list((env.sink_base / "profiles").glob("*.json"))) == 1
        for s in (S1, S2):
            assert _registrations(_events(env.sink_for(s)))[0]["profile_fingerprint"] == fa

    def test_rows50_52_no_full_hash_in_public_trace_fingerprint_is_twelve_hex(self, env: Env):
        _run(env, _pre(S1, "use-1"))
        _run(env, _post(S1, "use-1"))
        sink = env.sink_for(S1)
        for line in sink.read_text(encoding="utf-8").splitlines():
            assert not HEX64.search(line), line
        for fp in _fingerprints(_events(sink)):
            assert fp is not None and HEX12.match(fp)
        entry = read_session_binding(sink, S1)
        assert entry["profile_fingerprint"] == entry["profile_content_hash"][:12]

    def test_no_profile_anywhere_keeps_the_pre_profile_trace_shape(self, env: Env):
        env.resolution.unlink()
        _run(env, _pre(S1, "use-1"))
        _run(env, _post(S1, "use-1"))
        sink = env.sink_for(S1)
        events = _events(sink)
        assert all("profile_fingerprint" not in e for e in events)
        entry = read_session_binding(sink, S1)
        assert entry["resolution"] == "none" and entry["profile_content_hash"] is None
        assert not (env.sink_base / "profiles").exists()


# ---------------------------------------------------------------------------
# row 15: MCP wrapper and LangChain resolve once, per agent, no binding state
# ---------------------------------------------------------------------------


class _CapturingSink:
    def __init__(self) -> None:
        self.events: List[GovernanceEvent] = []

    def write(self, event: GovernanceEvent) -> bool:
        self.events.append(event)
        return True


class _Client:
    def invoke(self, name: str, args: dict) -> dict:
        return {"result": f"ok:{name}"}


class TestAdaptersResolvePerAgent:
    @pytest.mark.asyncio
    async def test_row15_mcp_wrapper_uses_resolved_profile(self, env: Env, tmp_path: Path):
        from sentience_governor.wrapper.mcp import SentienceMCPAdapter, wrap_mcp_client

        env.write_resolution(("mcp-agent-*", "profiles/B.yaml"))
        fb = env.fingerprint_of("B.yaml")
        captured = _CapturingSink()
        wrapped = wrap_mcp_client(
            target=SentienceMCPAdapter(delegate=_Client(), call_fn=lambda c, n, a: c.invoke(n, a)),
            session_manager=SessionManager(),
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            agent_id="mcp-agent-1",
            stated_objective="test",
            declared_capabilities=["crm.read"],
            session_id="sess-mcp-1",
        )
        async with wrapped:
            env.write_resolution(("mcp-agent-*", "profiles/A.yaml"))  # ignored: sticky
            wrapped.send_tool_call("crm.fetch", {"id": "1"})
        dumped = [e.to_dict() for e in captured.events]
        assert dumped[0]["event_type"] == EventType.AGENT_REGISTERED.value
        assert {e.get("profile_fingerprint") for e in dumped} == {fb}
        assert not list(tmp_path.rglob("*.sidecar*")) and not list(tmp_path.rglob("profiles/*.json"))

    @pytest.mark.asyncio
    async def test_row15_mcp_wrapper_unbound_agent_falls_to_default(self, env: Env):
        from sentience_governor.wrapper.mcp import SentienceMCPAdapter, wrap_mcp_client

        env.default.write_text(PROFILE_D, encoding="utf-8")
        fd = GovernanceProfile.from_file(env.default).fingerprint()
        captured = _CapturingSink()
        wrapped = wrap_mcp_client(
            target=SentienceMCPAdapter(delegate=_Client(), call_fn=lambda c, n, a: c.invoke(n, a)),
            session_manager=SessionManager(),
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            agent_id="other-agent",
            declared_capabilities=["crm.read"],
        )
        async with wrapped:
            pass
        assert captured.events[0].to_dict()["profile_fingerprint"] == fd

    def test_row15_langchain_handler_uses_resolved_profile(self, env: Env):
        from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler

        env.write_resolution(("lc-agent", "profiles/B.yaml"))
        fb = env.fingerprint_of("B.yaml")
        captured = _CapturingSink()
        handler = SentienceCallbackHandler(
            agent_id="lc-agent",
            session_manager=SessionManager(),
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            declared_capabilities=["test.read"],
        )
        handler.on_chain_start({}, {"input": "do the thing"})
        env.write_resolution(("lc-agent", "profiles/A.yaml"))  # ignored: sticky
        handler.on_tool_start({"name": "search"}, "query")
        dumped = [e.to_dict() for e in captured.events]
        assert dumped[0]["event_type"] == EventType.AGENT_REGISTERED.value
        assert {e.get("profile_fingerprint") for e in dumped} == {fb}
