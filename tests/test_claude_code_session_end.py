"""CP2 tests — live tool_use_id capture + SessionEnd token batch (v0.2.6.1).

Proves the CP2 deliverables against fixtures/tmp transcripts:

  * tool_use_id is persisted onto SCOPE_ASSERTED + the pre/post
    CONTEXT_SNAPSHOTs (and omitted when absent — no schema bloat).
  * SessionEnd parses the transcript and appends one token-bearing
    CONTEXT_SNAPSHOT per requestId (llm_turn_id, canonical tokens,
    tool_use_ids, provider, model), carrying NO policy violations.
  * no-tool-call turns still emit burn (D7).
  * the batch is idempotent (repeat SessionEnd does not double-emit).
  * fail-open: missing transcript_path / missing file never raise and never
    break the chain.
  * the appended snapshots chain cleanly (sequence monotonic).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sentience_governor.schema.events import EventType
from sentience_governor.wrapper.claude_code_hook import ClaudeCodeGovernanceHook


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def sink_path(tmp_path: Path) -> Path:
    return tmp_path / "trace.jsonl"


def _run(payload: dict, sink: Path) -> None:
    ClaudeCodeGovernanceHook(payload, sink).process()


def _read_events(sink: Path) -> List[dict]:
    if not sink.exists():
        return []
    return [
        json.loads(line)
        for line in sink.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


SESSION = "sess-cp2-001"


def _pre(tool: str, tool_use_id="use-1", session=SESSION, **inp) -> dict:
    p = {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": tool,
        "tool_input": inp or {"command": "noop"},
        "cwd": "/tmp",
    }
    if tool_use_id is not None:
        p["tool_use_id"] = tool_use_id
    return p


def _assistant(request_id, *, tool_uses=None, usage=None, model="claude-anon"):
    content: List[Dict[str, Any]] = []
    for tu in tool_uses or []:
        content.append({"type": "tool_use", "id": tu[0], "name": tu[1], "input": {}})
    msg: Dict[str, Any] = {"role": "assistant", "model": model, "content": content}
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "requestId": request_id, "message": msg}


def _usage(input_tokens=0, cache_write=0, cache_read=0, output=0):
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
    }


def _write_transcript(path: Path, lines: List[dict]) -> None:
    path.write_text("\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8")


def _session_end(transcript_path: Path, session=SESSION) -> dict:
    return {
        "hook_event_name": "SessionEnd",
        "session_id": session,
        "transcript_path": str(transcript_path),
        "cwd": "/tmp",
    }


def _token_snapshots(events: List[dict]) -> List[dict]:
    """Token-bearing snapshots = CONTEXT_SNAPSHOT carrying llm_turn_id."""
    return [
        e
        for e in events
        if e["event_type"] == EventType.CONTEXT_SNAPSHOT.value
        and e["payload"].get("llm_turn_id")
    ]


# ---------------------------------------------------------------------------
# Live tool_use_id capture.
# ---------------------------------------------------------------------------


class TestLiveToolUseIdCapture:
    def test_scope_and_pre_context_carry_tool_use_id(self, sink_path: Path):
        _run(_pre("Bash", tool_use_id="toolu_LIVE1", command="ls"), sink_path)
        events = _read_events(sink_path)
        scope = next(e for e in events if e["event_type"] == EventType.SCOPE_ASSERTED.value)
        ctx = next(e for e in events if e["event_type"] == EventType.CONTEXT_SNAPSHOT.value)
        assert scope["payload"]["tool_use_id"] == "toolu_LIVE1"
        assert ctx["payload"]["tool_use_id"] == "toolu_LIVE1"

    def test_post_context_carries_tool_use_id(self, sink_path: Path):
        _run(_pre("Read", tool_use_id="toolu_LIVE2", file_path="/x"), sink_path)
        _run(
            {
                "hook_event_name": "PostToolUse",
                "session_id": SESSION,
                "tool_name": "Read",
                "tool_input": {"file_path": "/x"},
                "tool_response": {"content": "hi"},
                "tool_use_id": "toolu_LIVE2",
            },
            sink_path,
        )
        events = _read_events(sink_path)
        post = events[-1]
        assert post["event_type"] == EventType.CONTEXT_SNAPSHOT.value
        assert post["payload"]["tool_use_id"] == "toolu_LIVE2"

    def test_tool_use_id_omitted_when_absent(self, sink_path: Path):
        # Older Claude Code without tool_use_id → field never appears (no bloat).
        _run(_pre("Bash", tool_use_id=None, command="ls"), sink_path)
        events = _read_events(sink_path)
        scope = next(e for e in events if e["event_type"] == EventType.SCOPE_ASSERTED.value)
        assert "tool_use_id" not in scope["payload"]


# ---------------------------------------------------------------------------
# SessionEnd token batch.
# ---------------------------------------------------------------------------


class TestSessionEndBatch:
    @pytest.fixture
    def transcript(self, tmp_path: Path) -> Path:
        path = tmp_path / "transcript.jsonl"
        _write_transcript(
            path,
            [
                {"type": "user", "message": {"role": "user", "content": "go"}},
                _assistant(
                    "req_A",
                    tool_uses=[("toolu_A1", "Read")],
                    usage=_usage(2, 330, 38631, 319),
                ),
                # D7: a pure-answer turn, no tool call, real burn.
                _assistant("req_C", usage=_usage(5, 200, 0, 400)),
            ],
        )
        return path

    def test_emits_one_snapshot_per_request(self, sink_path: Path, transcript: Path):
        _run(_pre("Read", tool_use_id="toolu_A1"), sink_path)
        _run(_session_end(transcript), sink_path)
        snaps = _token_snapshots(_read_events(sink_path))
        turn_ids = {s["payload"]["llm_turn_id"] for s in snaps}
        assert turn_ids == {"req_A", "req_C"}

    def test_snapshot_carries_canonical_tokens_and_join_keys(
        self, sink_path: Path, transcript: Path
    ):
        _run(_pre("Read", tool_use_id="toolu_A1"), sink_path)
        _run(_session_end(transcript), sink_path)
        snaps = _token_snapshots(_read_events(sink_path))
        a = next(s for s in snaps if s["payload"]["llm_turn_id"] == "req_A")
        pl = a["payload"]
        assert pl["llm_prompt_tokens"] == 2
        assert pl["llm_completion_tokens"] == 319
        assert pl["llm_cached_read_tokens"] == 38631
        assert pl["llm_cached_write_tokens"] == 330
        assert pl["tool_use_ids"] == ["toolu_A1"]
        assert pl["provider"] == "anthropic"
        assert pl["model_identifier"] == "claude-anon"
        # context_size_tokens = the turn's anthropic-additive burn.
        assert pl["context_size_tokens"] == 2 + 319 + 38631 + 330

    def test_no_tool_turn_emitted_without_tool_use_ids(
        self, sink_path: Path, transcript: Path
    ):
        _run(_session_end(transcript), sink_path)
        snaps = _token_snapshots(_read_events(sink_path))
        c = next(s for s in snaps if s["payload"]["llm_turn_id"] == "req_C")
        assert "tool_use_ids" not in c["payload"]  # no tools → omitted
        assert c["payload"]["llm_completion_tokens"] == 400

    def test_token_snapshots_carry_no_policy_violations(
        self, sink_path: Path, transcript: Path
    ):
        # The load-bearing correctness check: token carriers must NOT
        # manufacture POL-003 just because they are "unclassified".
        _run(_session_end(transcript), sink_path)
        for s in _token_snapshots(_read_events(sink_path)):
            assert s.get("policy_violations", []) == []

    def test_chain_stays_monotonic(self, sink_path: Path, transcript: Path):
        _run(_pre("Read", tool_use_id="toolu_A1"), sink_path)
        _run(_session_end(transcript), sink_path)
        events = _read_events(sink_path)
        for i, ev in enumerate(events):
            assert ev["event_sequence_number"] == i + 1


# ---------------------------------------------------------------------------
# Idempotency + fail-open.
# ---------------------------------------------------------------------------


class TestIdempotencyAndFailOpen:
    @pytest.fixture
    def transcript(self, tmp_path: Path) -> Path:
        path = tmp_path / "t.jsonl"
        _write_transcript(
            path,
            [_assistant("req_A", tool_uses=[("toolu_A1", "Read")], usage=_usage(1, 0, 0, 9))],
        )
        return path

    def test_repeat_session_end_does_not_double_emit(
        self, sink_path: Path, transcript: Path
    ):
        _run(_session_end(transcript), sink_path)
        first = len(_token_snapshots(_read_events(sink_path)))
        _run(_session_end(transcript), sink_path)
        second = len(_token_snapshots(_read_events(sink_path)))
        assert first == 1
        assert second == 1  # sidecar idempotency prevented a duplicate

    def test_missing_transcript_path_is_fail_open(self, sink_path: Path):
        # SessionEnd with no transcript_path → no token snapshots, no crash.
        _run({"hook_event_name": "SessionEnd", "session_id": SESSION}, sink_path)
        assert _token_snapshots(_read_events(sink_path)) == []

    def test_missing_transcript_file_is_fail_open(
        self, sink_path: Path, tmp_path: Path
    ):
        _run(_session_end(tmp_path / "does_not_exist.jsonl"), sink_path)
        assert _token_snapshots(_read_events(sink_path)) == []

    def test_malformed_transcript_partial_success(
        self, sink_path: Path, tmp_path: Path
    ):
        path = tmp_path / "partial.jsonl"
        good = json.dumps(_assistant("req_G", usage=_usage(1, 0, 0, 2)))
        path.write_text(good + "\n{ truncated tail", encoding="utf-8")
        _run(_session_end(path), sink_path)
        snaps = _token_snapshots(_read_events(sink_path))
        # The complete turn still emits; the truncated tail is skipped.
        assert {s["payload"]["llm_turn_id"] for s in snaps} == {"req_G"}
