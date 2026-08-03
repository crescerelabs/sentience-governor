"""Acceptance tests for the Claude Code hook adapter.

Covers 15 Claude Code integration cases:

1.  Canned PreToolUse payload for Bash produces a valid SCOPE_ASSERTED
    event with correct operation_type / tool_id / target_system.
2.  Post-call events chain from pre-call events (sequence + previous_event_id).
3.  First event in a new session emits AGENT_REGISTERED + INTENT_DECLARED.
4.  Multi-invocation session: sequence numbers monotonic; chain unbroken.
5.  Multi-session interleaving: each session has its own chain.
6.  Operation-type mapping for every built-in tool.
7.  MCP tool name parsing (target + operation type).
8.  Persistence target firing: Write fires MEMORY_WRITE_ATTEMPT, Read does not.
9.  Fail-open on malformed JSON on stdin.
10. Fail-open on sink write failure.
11. Sidecar drift detection + repair (stale offset → linear-scan rebuild).
12. Missing sidecar recovery.
13. Corrupt sidecar recovery.
14. Atomic sidecar write (crash between temp and rename leaves old intact).
15. Lock covers reads (not just writes).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List

import pytest

from sentience_governor.schema.events import EventType, OperationType
from sentience_governor.session_manager.resumption import (
    _write_sidecar_atomic,
    resume_session_state,
    sidecar_path_for,
    update_session_state,
)
from sentience_governor.wrapper.claude_code_hook import (
    ClaudeCodeGovernanceHook,
    _BUILTIN_TOOL_MAP,
    _infer_tool_mapping,
    _is_persistence_target,
    _resolve_sink_base,
    _session_file_for,
    run_hook,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sink_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.jsonl"


def _pre_payload(tool: str, session: str = "sess-abc123xyz", **input_fields) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": input_fields or {"command": "noop"},
        "tool_use_id": "use-1",
        "cwd": "/tmp",
    }


def _post_payload(tool: str, session: str = "sess-abc123xyz", response=None) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": {"command": "noop"},
        "tool_response": response if response is not None else {"ok": True},
        "tool_use_id": "use-1",
        "cwd": "/tmp",
    }


def _read_events(sink: Path) -> List[dict]:
    if not sink.exists():
        return []
    out: List[dict] = []
    for line in sink.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _run(payload: dict, sink: Path) -> None:
    ClaudeCodeGovernanceHook(payload, sink).process()


# ---------------------------------------------------------------------------
# 1. Canned PreToolUse for Bash → SCOPE_ASSERTED is correct
# ---------------------------------------------------------------------------


class TestSingleInvocation:
    def test_pre_bash_emits_expected_events(self, sink_path: Path):
        _run(_pre_payload("Bash", command="ls -la"), sink_path)
        events = _read_events(sink_path)
        # Expect: REG, INTENT, SCOPE, CONTEXT (Bash is not a persistence target)
        types = [e["event_type"] for e in events]
        assert types == [
            EventType.AGENT_REGISTERED.value,
            EventType.INTENT_DECLARED.value,
            EventType.SCOPE_ASSERTED.value,
            EventType.CONTEXT_SNAPSHOT.value,
        ]
        scope = events[2]
        assert scope["payload"]["tool_id"] == "Bash"
        assert scope["payload"]["target_system"] == "shell"
        assert scope["payload"]["operation_type"] == OperationType.EXECUTE.value


# ---------------------------------------------------------------------------
# 2. Chain integrity: post-call follows pre-call
# 3. First event emits REG + INTENT; later events do not
# 4. Multi-invocation session: sequence + chain
# ---------------------------------------------------------------------------


class TestChainIntegrity:
    def test_pre_then_post_chains(self, sink_path: Path):
        _run(_pre_payload("Read", file_path="/tmp/x"), sink_path)
        _run(_post_payload("Read", response={"content": "hi"}), sink_path)
        events = _read_events(sink_path)

        # REG, INTENT, SCOPE, CONTEXT(pre), CONTEXT(post)
        assert len(events) == 5
        for i, ev in enumerate(events):
            assert ev["event_sequence_number"] == i + 1
        # Chain: each event's previous_event_id matches prior event_id
        for i in range(1, len(events)):
            assert events[i]["previous_event_id"] == events[i - 1]["event_id"]
        assert events[0]["previous_event_id"] is None

    def test_reg_intent_only_on_first_invocation(self, sink_path: Path):
        _run(_pre_payload("Read", file_path="/a"), sink_path)
        _run(_pre_payload("Read", file_path="/b"), sink_path)
        events = _read_events(sink_path)
        reg_count = sum(
            1 for e in events if e["event_type"] == EventType.AGENT_REGISTERED.value
        )
        intent_count = sum(
            1 for e in events if e["event_type"] == EventType.INTENT_DECLARED.value
        )
        assert reg_count == 1
        assert intent_count == 1

    def test_five_invocations_monotonic_chain(self, sink_path: Path):
        for i in range(5):
            _run(_pre_payload("Read", file_path=f"/f{i}"), sink_path)
        events = _read_events(sink_path)
        seqs = [e["event_sequence_number"] for e in events]
        assert seqs == list(range(1, len(events) + 1))
        for i in range(1, len(events)):
            assert events[i]["previous_event_id"] == events[i - 1]["event_id"]


# ---------------------------------------------------------------------------
# 5. Multi-session interleaving
# ---------------------------------------------------------------------------


class TestMultiSession:
    def test_interleaved_sessions_have_independent_chains(self, sink_path: Path):
        _run(_pre_payload("Read", session="sess-A-abcd", file_path="/a"), sink_path)
        _run(_pre_payload("Read", session="sess-B-efgh", file_path="/b"), sink_path)
        _run(_pre_payload("Read", session="sess-A-abcd", file_path="/a2"), sink_path)
        _run(_pre_payload("Read", session="sess-B-efgh", file_path="/b2"), sink_path)
        events = _read_events(sink_path)

        a = [e for e in events if e["session_id"] == "sess-A-abcd"]
        b = [e for e in events if e["session_id"] == "sess-B-efgh"]

        assert [e["event_sequence_number"] for e in a] == list(range(1, len(a) + 1))
        assert [e["event_sequence_number"] for e in b] == list(range(1, len(b) + 1))

        for chain in (a, b):
            assert chain[0]["previous_event_id"] is None
            for i in range(1, len(chain)):
                assert chain[i]["previous_event_id"] == chain[i - 1]["event_id"]


# ---------------------------------------------------------------------------
# 6. Operation-type mapping for every built-in tool
# ---------------------------------------------------------------------------


class TestToolMapping:
    @pytest.mark.parametrize("tool_name,expected_op,expected_target", [
        ("Bash", OperationType.EXECUTE, "shell"),
        ("Edit", OperationType.WRITE, "filesystem"),
        ("Write", OperationType.WRITE, "filesystem"),
        ("NotebookEdit", OperationType.WRITE, "filesystem"),
        ("Read", OperationType.READ, "filesystem"),
        ("Grep", OperationType.READ, "filesystem"),
        ("Glob", OperationType.READ, "filesystem"),
        ("WebFetch", OperationType.READ, "web"),
        ("WebSearch", OperationType.READ, "web"),
        ("Agent", OperationType.EXECUTE, "agent_runtime"),
    ])
    def test_builtin_tool_mapping(self, tool_name, expected_op, expected_target):
        op, target = _infer_tool_mapping(tool_name)
        assert op == expected_op
        assert target == expected_target

    def test_every_builtin_in_table_covered(self):
        # Regression guard: if a new tool is added to the map, ensure it
        # has a plausible (OperationType, target_system) pair.
        for tool, (op, target) in _BUILTIN_TOOL_MAP.items():
            assert isinstance(op, OperationType)
            assert isinstance(target, str) and target


# ---------------------------------------------------------------------------
# 7. MCP tool name parsing
# ---------------------------------------------------------------------------


class TestMCPToolParsing:
    def test_read_mcp_tool(self):
        op, target = _infer_tool_mapping("mcp__airtable__search_records")
        assert target == "airtable"
        assert op == OperationType.READ

    def test_write_mcp_tool_infers_write_op(self):
        op, target = _infer_tool_mapping("mcp__db__write_snapshot_to_database")
        assert target == "db"
        assert op == OperationType.WRITE

    def test_execute_mcp_tool_infers_execute_op(self):
        op, target = _infer_tool_mapping("mcp__sandbox__run_command")
        assert target == "sandbox"
        assert op == OperationType.EXECUTE

    def test_unknown_tool_defaults_to_read(self):
        op, target = _infer_tool_mapping("SomeCustomTool")
        assert op == OperationType.READ
        assert target == "SomeCustomTool"


# ---------------------------------------------------------------------------
# 8. Persistence target firing
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_write_fires_memory_write_attempt(self, sink_path: Path):
        _run(_pre_payload("Write", file_path="/x.txt", content="abc"), sink_path)
        events = _read_events(sink_path)
        types = [e["event_type"] for e in events]
        assert EventType.MEMORY_WRITE_ATTEMPT.value in types

    def test_read_does_not_fire_memory_write(self, sink_path: Path):
        _run(_pre_payload("Read", file_path="/x.txt"), sink_path)
        events = _read_events(sink_path)
        types = [e["event_type"] for e in events]
        assert EventType.MEMORY_WRITE_ATTEMPT.value not in types

    def test_bash_does_not_fire_memory_write(self, sink_path: Path):
        # Known blind spot: Bash can persist, but we don't introspect.
        _run(_pre_payload("Bash", command="rm -rf /tmp/x"), sink_path)
        events = _read_events(sink_path)
        types = [e["event_type"] for e in events]
        assert EventType.MEMORY_WRITE_ATTEMPT.value not in types

    def test_is_persistence_target_helper(self):
        # With explicit operation_type: WRITE+filesystem fires; READ+filesystem does not.
        assert _is_persistence_target("filesystem", "Edit", OperationType.WRITE)
        assert not _is_persistence_target("filesystem", "Read", OperationType.READ)
        assert _is_persistence_target(
            "db", "mcp__db__write_snapshot_to_database", OperationType.WRITE
        )
        # Bash (EXECUTE + shell): neither keyword match nor WRITE-class op.
        assert not _is_persistence_target("shell", "Bash", OperationType.EXECUTE)
        assert not _is_persistence_target("web", "WebFetch", OperationType.READ)


# ---------------------------------------------------------------------------
# 9. Fail-open on malformed input
# 10. Fail-open on sink write failure
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_malformed_json_on_stdin_exits_zero(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(
            "SENTIENCE_CLAUDE_CODE_SINK_PATH", str(tmp_path / "trace.jsonl")
        )
        monkeypatch.setattr("sys.stdin", _FakeStdin("not valid json {{{"))
        code = run_hook()
        assert code == 0

    def test_empty_stdin_exits_zero(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(
            "SENTIENCE_CLAUDE_CODE_SINK_PATH", str(tmp_path / "trace.jsonl")
        )
        monkeypatch.setattr("sys.stdin", _FakeStdin(""))
        code = run_hook()
        assert code == 0

    def test_non_object_payload_exits_zero(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv(
            "SENTIENCE_CLAUDE_CODE_SINK_PATH", str(tmp_path / "trace.jsonl")
        )
        monkeypatch.setattr("sys.stdin", _FakeStdin('["list", "not", "object"]'))
        code = run_hook()
        assert code == 0

    def test_sink_write_failure_does_not_raise(self, sink_path: Path):
        # Make the sink path a directory so file writes fail.
        sink_path.mkdir()
        # Should not raise:
        _run(_pre_payload("Bash"), sink_path)

    def test_missing_session_id_is_noop(self, sink_path: Path):
        _run({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, sink_path)
        assert not sink_path.exists() or sink_path.read_text() == ""

    def test_unknown_hook_event_is_noop_for_existing_session(self, sink_path: Path):
        # First a normal invocation to establish a session
        _run(_pre_payload("Read", file_path="/a"), sink_path)
        before = _read_events(sink_path)
        # Unknown hook event should not add events
        _run({
            "hook_event_name": "SessionEnd",
            "session_id": "sess-abc123xyz",
            "tool_name": "",
        }, sink_path)
        after = _read_events(sink_path)
        assert len(after) == len(before)


class _FakeStdin:
    def __init__(self, data: str) -> None:
        self._data = data

    def read(self) -> str:
        return self._data


# ---------------------------------------------------------------------------
# 11. Sidecar drift repair
# ---------------------------------------------------------------------------


class TestSidecarDrift:
    def test_stale_sidecar_falls_back_to_scan_and_repairs(self, sink_path: Path):
        # Build a trace with 5 events
        for i in range(5):
            _run(_pre_payload("Read", file_path=f"/f{i}"), sink_path)

        sidecar = sidecar_path_for(sink_path)
        assert sidecar.exists()

        # Corrupt the sidecar: point at offset 0 with wrong event_id
        _write_sidecar_atomic(
            sidecar,
            {
                "sess-abc123xyz": {
                    "last_sequence": 999,
                    "last_event_id": "evt-bogus",
                    "file_offset": 0,
                }
            },
        )

        # Resumption should detect drift and rebuild via scan
        resumed = resume_session_state(sink_path, "sess-abc123xyz")
        assert resumed is not None
        # The true last event is the last CONTEXT in the trace
        events = _read_events(sink_path)
        assert resumed.last_event_id == events[-1]["event_id"]
        assert resumed.last_sequence == events[-1]["event_sequence_number"]

    def test_next_invocation_after_drift_chains_correctly(self, sink_path: Path):
        # Build, corrupt sidecar, then invoke once more — new event must
        # chain to the true last event, not the sidecar's bogus entry.
        for i in range(3):
            _run(_pre_payload("Read", file_path=f"/f{i}"), sink_path)
        sidecar = sidecar_path_for(sink_path)
        _write_sidecar_atomic(
            sidecar,
            {
                "sess-abc123xyz": {
                    "last_sequence": 99,
                    "last_event_id": "evt-bogus",
                    "file_offset": 0,
                }
            },
        )
        events_before = _read_events(sink_path)
        _run(_pre_payload("Read", file_path="/f-new"), sink_path)
        events_after = _read_events(sink_path)
        new_events = events_after[len(events_before):]
        assert new_events  # something was appended
        # The first newly-appended event must chain to the true last
        # pre-existing event, not the bogus sidecar entry.
        assert new_events[0]["previous_event_id"] == events_before[-1]["event_id"]
        assert (
            new_events[0]["event_sequence_number"]
            == events_before[-1]["event_sequence_number"] + 1
        )


# ---------------------------------------------------------------------------
# 12. Missing sidecar recovery
# ---------------------------------------------------------------------------


class TestMissingSidecar:
    def test_missing_sidecar_rebuilds_from_scan(self, sink_path: Path):
        for i in range(3):
            _run(_pre_payload("Read", file_path=f"/f{i}"), sink_path)
        sidecar = sidecar_path_for(sink_path)
        sidecar.unlink()
        assert not sidecar.exists()

        # Next invocation should rebuild without error
        _run(_pre_payload("Read", file_path="/f-new"), sink_path)
        events = _read_events(sink_path)
        # Chain integrity preserved
        for i in range(1, len(events)):
            assert events[i]["previous_event_id"] == events[i - 1]["event_id"]
        # Sidecar regenerated
        assert sidecar.exists()


# ---------------------------------------------------------------------------
# 13. Corrupt sidecar recovery
# ---------------------------------------------------------------------------


class TestCorruptSidecar:
    def test_half_written_json_is_treated_as_missing(self, sink_path: Path):
        for i in range(2):
            _run(_pre_payload("Read", file_path=f"/f{i}"), sink_path)
        sidecar = sidecar_path_for(sink_path)
        sidecar.write_text('{"sess-abc123xyz": {"last_seq', encoding="utf-8")

        # Should not raise; should rebuild
        _run(_pre_payload("Read", file_path="/f-new"), sink_path)
        events = _read_events(sink_path)
        for i in range(1, len(events)):
            assert events[i]["previous_event_id"] == events[i - 1]["event_id"]

    def test_non_dict_sidecar_treated_as_missing(self, sink_path: Path):
        for i in range(2):
            _run(_pre_payload("Read", file_path=f"/f{i}"), sink_path)
        sidecar = sidecar_path_for(sink_path)
        sidecar.write_text('["not", "a", "dict"]', encoding="utf-8")

        _run(_pre_payload("Read", file_path="/f-new"), sink_path)
        events = _read_events(sink_path)
        for i in range(1, len(events)):
            assert events[i]["previous_event_id"] == events[i - 1]["event_id"]


# ---------------------------------------------------------------------------
# 14. Atomic sidecar write
# ---------------------------------------------------------------------------


class TestAtomicSidecar:
    def test_crash_before_rename_leaves_old_intact(
        self, sink_path: Path, monkeypatch
    ):
        # Seed a valid sidecar via a real invocation
        _run(_pre_payload("Read", file_path="/a"), sink_path)
        sidecar = sidecar_path_for(sink_path)
        original = sidecar.read_text(encoding="utf-8")

        # Monkeypatch os.rename inside the resumption module to simulate
        # a crash between the temp write and the rename.
        import sentience_governor.session_manager.resumption as rm
        def boom(*args, **kwargs):
            raise OSError("simulated crash")
        monkeypatch.setattr(rm.os, "rename", boom)

        # An attempted update should raise inside the writer, but the
        # original sidecar file content must remain intact. The hook
        # itself swallows this at a higher layer; here we test the
        # primitive directly.
        with pytest.raises(OSError):
            rm._write_sidecar_atomic(sidecar, {"new": "state"})

        assert sidecar.read_text(encoding="utf-8") == original
        # The temp file may or may not exist but it's not the sidecar.
        tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
        # Cleanup if left behind
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# 15. Lock covers reads
# ---------------------------------------------------------------------------


class TestLocking:
    def test_sink_lock_blocks_concurrent_acquirers(
        self, sink_path: Path, tmp_path: Path
    ):
        # Two subprocesses, each acquiring the lock and holding briefly.
        # Total wall-time must be at least 2x the hold duration because
        # they cannot overlap under the lock.
        import time

        sink_path.touch()
        script_file = tmp_path / "lock_probe.py"
        script_file.write_text(
            "import os, sys, time\n"
            "from pathlib import Path\n"
            "from sentience_governor.session_manager.resumption import sink_lock\n"
            f"p = Path({str(sink_path)!r})\n"
            "hold = float(os.environ.get('HOLD', '0.3'))\n"
            "with sink_lock(p):\n"
            "    time.sleep(hold)\n",
            encoding="utf-8",
        )
        env = {**os.environ, "HOLD": "0.3"}
        t0 = time.monotonic()
        p1 = subprocess.Popen(
            [sys.executable, str(script_file)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        p2 = subprocess.Popen(
            [sys.executable, str(script_file)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out1, err1 = p1.communicate(timeout=10)
        out2, err2 = p2.communicate(timeout=10)
        elapsed = time.monotonic() - t0

        # Both probes must succeed — a non-zero exit means the probe
        # crashed before acquiring the lock, which would make the
        # elapsed-time assertion meaningless.
        assert p1.returncode == 0, f"probe 1 failed: {err1!r}"
        assert p2.returncode == 0, f"probe 2 failed: {err2!r}"

        # With the lock, the two holders serialize: total wall time >= 2*hold
        # (minus a small overlap tolerance for startup scheduling).
        assert elapsed >= 0.5, f"expected >=0.5s serialization, got {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Bonus: update_session_state round-trip
# ---------------------------------------------------------------------------


class TestSidecarRoundTrip:
    def test_update_then_resume(self, sink_path: Path):
        # Hand-write a valid trace line at a known offset
        line = json.dumps({
            "event_id": "evt-xyz",
            "session_id": "sess-A",
            "event_sequence_number": 42,
            "event_type": "AGENT_REGISTERED",
        }) + "\n"
        sink_path.write_text(line, encoding="utf-8")

        update_session_state(
            sink_path=sink_path,
            session_id="sess-A",
            last_sequence=42,
            last_event_id="evt-xyz",
            file_offset=0,
        )
        resumed = resume_session_state(sink_path, "sess-A")
        assert resumed is not None
        assert resumed.last_sequence == 42
        assert resumed.last_event_id == "evt-xyz"
        assert resumed.file_offset == 0


# ---------------------------------------------------------------------------
# Per-session sink path resolution (new default behaviour in this release)
# ---------------------------------------------------------------------------


class TestPerSessionSinkPath:
    def test_unset_env_defaults_to_directory_mode(self, monkeypatch):
        monkeypatch.delenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", raising=False)
        base, shared = _resolve_sink_base()
        assert shared is False
        # Default directory lives under ~/.sentience/traces/claude-code
        assert base.name == "claude-code"
        assert base.suffix == ""

    def test_env_pointing_at_jsonl_file_is_shared_mode(self, monkeypatch):
        monkeypatch.setenv(
            "SENTIENCE_CLAUDE_CODE_SINK_PATH", "/tmp/explicit-shared.jsonl"
        )
        base, shared = _resolve_sink_base()
        assert shared is True
        assert str(base) == "/tmp/explicit-shared.jsonl"

    def test_env_pointing_at_directory_is_per_session_mode(self, monkeypatch):
        monkeypatch.setenv(
            "SENTIENCE_CLAUDE_CODE_SINK_PATH", "/tmp/explicit-dir"
        )
        base, shared = _resolve_sink_base()
        assert shared is False
        assert str(base) == "/tmp/explicit-dir"

    def test_session_file_for_directory_uses_session_id_as_filename(
        self, tmp_path: Path
    ):
        resolved = _session_file_for(tmp_path, False, "sess-abc-xyz")
        assert resolved == tmp_path / "sess-abc-xyz.jsonl"
        assert tmp_path.exists()  # parent auto-created

    def test_session_file_for_shared_mode_returns_base_file(
        self, tmp_path: Path
    ):
        shared_file = tmp_path / "shared.jsonl"
        resolved = _session_file_for(shared_file, True, "sess-anything")
        # Shared mode: session_id does NOT influence the path.
        assert resolved == shared_file

    def test_per_session_routing_produces_separate_files(
        self, tmp_path: Path
    ):
        # Two sessions via the hook (sink_base + shared_file_mode=False).
        # Each must land in its own file.
        hook_a = ClaudeCodeGovernanceHook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-A-12345678",
                "tool_name": "Read",
                "tool_input": {"file_path": "/a"},
            },
            sink_base=tmp_path,
            shared_file_mode=False,
        )
        hook_a.process()

        hook_b = ClaudeCodeGovernanceHook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-B-abcdefgh",
                "tool_name": "Read",
                "tool_input": {"file_path": "/b"},
            },
            sink_base=tmp_path,
            shared_file_mode=False,
        )
        hook_b.process()

        file_a = tmp_path / "sess-A-12345678.jsonl"
        file_b = tmp_path / "sess-B-abcdefgh.jsonl"
        assert file_a.exists()
        assert file_b.exists()

        events_a = [json.loads(line) for line in file_a.read_text().splitlines() if line]
        events_b = [json.loads(line) for line in file_b.read_text().splitlines() if line]

        # Each file holds only its own session.
        assert all(e["session_id"] == "sess-A-12345678" for e in events_a)
        assert all(e["session_id"] == "sess-B-abcdefgh" for e in events_b)

        # Each file starts fresh: seq=1 is AGENT_REGISTERED.
        assert events_a[0]["event_sequence_number"] == 1
        assert events_b[0]["event_sequence_number"] == 1

    def test_shared_mode_interleaves_sessions_in_one_file(
        self, tmp_path: Path
    ):
        # Back-compat: a .jsonl path → shared-file mode.
        shared_file = tmp_path / "shared.jsonl"
        for sess in ("sess-X-11111111", "sess-Y-22222222"):
            ClaudeCodeGovernanceHook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": sess,
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/x"},
                },
                sink_base=shared_file,
                shared_file_mode=True,
            ).process()

        events = [
            json.loads(line)
            for line in shared_file.read_text().splitlines()
            if line
        ]
        session_ids = {e["session_id"] for e in events}
        assert session_ids == {"sess-X-11111111", "sess-Y-22222222"}


# ---------------------------------------------------------------------------
# Viewer directory rendering
# ---------------------------------------------------------------------------


class TestViewerDirectoryRender:
    def test_viewer_scans_directory_and_merges_sessions(
        self, tmp_path: Path
    ):
        # Produce two per-session files.
        for sess in ("sess-one-abcd1234", "sess-two-efgh5678"):
            ClaudeCodeGovernanceHook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": sess,
                    "tool_name": "Read",
                    "tool_input": {"file_path": "/x"},
                },
                sink_base=tmp_path,
                shared_file_mode=False,
            ).process()

        # Parse the directory via the viewer's internal helper.
        from sentience_governor.cli.viewer import _parse_events_from_directory

        sessions = _parse_events_from_directory(tmp_path)
        assert set(sessions.keys()) == {
            "sess-one-abcd1234",
            "sess-two-efgh5678",
        }
        for sess_id, events in sessions.items():
            assert all(e["session_id"] == sess_id for e in events)
            assert events[0]["event_sequence_number"] == 1

    def test_viewer_directory_ignores_sidecar_files(self, tmp_path: Path):
        ClaudeCodeGovernanceHook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sess-abcd1234",
                "tool_name": "Read",
                "tool_input": {"file_path": "/x"},
            },
            sink_base=tmp_path,
            shared_file_mode=False,
        ).process()

        # The sidecar (.jsonl.index) sits right next to the session file.
        # The viewer's glob('*.jsonl') must NOT match it.
        sidecar = tmp_path / "sess-abcd1234.jsonl.index"
        assert sidecar.exists()

        from sentience_governor.cli.viewer import _parse_events_from_directory

        sessions = _parse_events_from_directory(tmp_path)
        # Exactly one session, and it parsed cleanly (no sidecar contamination).
        assert list(sessions.keys()) == ["sess-abcd1234"]


# ---------------------------------------------------------------------------
# v0.2.3 Track 2 — token-data passthrough from hook payload
# ---------------------------------------------------------------------------


class TestTokenDataFromHookPayload:
    """The Claude Code hook adapter probes for ``usage`` / ``token_usage``
    in the payload. Today Anthropic doesn't surface this; the field
    stays None and tests verify graceful absence. The adapter is wired
    so that IF Anthropic adds it upstream, we pick it up.
    """

    def test_no_usage_in_payload_yields_omitted_token_fields(self, sink_path: Path):
        """Default case (today): no usage data in payload → CONTEXT_SNAPSHOT
        events emit without token fields (omitted by the central serializer)."""
        _run(_pre_payload("Bash", command="ls"), sink_path)
        events = _read_events(sink_path)
        ctx_events = [e for e in events if e["event_type"] == "CONTEXT_SNAPSHOT"]
        assert ctx_events
        for e in ctx_events:
            payload = e["payload"]
            # Token fields absent (None values omitted from serialised payload).
            assert "llm_prompt_tokens" not in payload
            assert "llm_turn_id" not in payload

    def test_usage_in_payload_populates_token_fields(self, sink_path: Path):
        """If Anthropic adds 'usage' to the hook payload (Anthropic-shape),
        we pick it up automatically."""
        payload = _pre_payload("Bash", command="ls")
        # Simulate a future Anthropic payload that surfaces token usage.
        payload["usage"] = {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 30,
        }
        _run(payload, sink_path)

        events = _read_events(sink_path)
        ctx_events = [e for e in events if e["event_type"] == "CONTEXT_SNAPSHOT"]
        assert ctx_events
        # The CONTEXT_SNAPSHOT payload should now carry the token fields.
        last = ctx_events[-1]["payload"]
        assert last.get("llm_prompt_tokens") == 100
        assert last.get("llm_completion_tokens") == 50
        assert last.get("llm_cached_read_tokens") == 30

    def test_malformed_usage_does_not_crash(self, sink_path: Path):
        """Defensive: a malformed usage value must not break event emission."""
        payload = _pre_payload("Bash", command="ls")
        payload["usage"] = "not a usage object"
        # Must not raise.
        _run(payload, sink_path)
        events = _read_events(sink_path)
        # Events still emitted; token fields just absent (all-None → omitted).
        ctx_events = [e for e in events if e["event_type"] == "CONTEXT_SNAPSHOT"]
        assert ctx_events
        for e in ctx_events:
            assert "llm_prompt_tokens" not in e["payload"]
