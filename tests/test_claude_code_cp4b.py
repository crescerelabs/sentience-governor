"""CP4b — D7 total-vs-attributable labeling, subagent-excluded notice (D5),
and scale/R5 (v0.2.6.1).

Drives the real hook to produce traces, then checks the analyzer field +
renderer disclosures, and exercises the full SessionEnd -> analyzers
pipeline at thousands of turns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sentience_governor.analyze.policy_violation_burn_rate import (
    compute_policy_violation_burn_rate,
)
from sentience_governor.analyze.pulse import compute_pulse
from sentience_governor.analyze.renderers import (
    render_burn_rate_cli,
    render_burn_rate_markdown,
    render_pulse_cli,
)
from sentience_governor.wrapper.claude_code_hook import ClaudeCodeGovernanceHook

SESSION = "sess-cp4b"


def _run(payload: dict, sink: Path) -> None:
    ClaudeCodeGovernanceHook(payload, sink).process()


def _read(sink: Path) -> List[dict]:
    return [json.loads(l) for l in sink.read_text().splitlines() if l.strip()]


def _pre(tool, tuid):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": SESSION,
        "tool_name": tool,
        "tool_input": {"file_path": "/x"},
        "tool_use_id": tuid,
    }


def _assistant(rid, tuid=None, usage=None):
    content = []
    if tuid:
        content.append({"type": "tool_use", "id": tuid, "name": "Edit", "input": {}})
    msg: Dict[str, Any] = {"role": "assistant", "model": "m", "content": content}
    if usage:
        msg["usage"] = usage
    return {"type": "assistant", "requestId": rid, "message": msg}


def _u(o):
    return {"input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": o}


def _session_end(path):
    return {"hook_event_name": "SessionEnd", "session_id": SESSION,
            "transcript_path": str(path)}


# ---------------------------------------------------------------------------
# D7 — total vs governance-attributable burn.
# ---------------------------------------------------------------------------


class TestD7Labeling:
    @pytest.fixture
    def result(self, tmp_path):
        sink = tmp_path / "t.jsonl"
        tr = tmp_path / "tr.jsonl"
        tr.write_text(
            "\n".join(json.dumps(o) for o in [
                _assistant("req_A", "toolu_A1", _u(100)),   # violation turn
                _assistant("req_C", usage=_u(30)),          # no-tool: D7 burn
            ]) + "\n"
        )
        _run(_pre("Edit", "toolu_A1"), sink)
        _run(_session_end(tr), sink)
        return compute_policy_violation_burn_rate(_read(sink))

    def test_total_exceeds_attributable(self, result):
        assert result["total_tokens"] == 130       # 100 (req_A) + 30 (req_C)
        assert result["violation_associated_tokens"] == 100  # req_A only
        assert result["status"] == "ok"

    def test_cli_discloses_unattributed_burn(self, result):
        out = render_burn_rate_cli(result, color=False)
        assert "Governance-attributable" in out
        assert "30" in out
        assert "not tied to a tool-call violation" in out

    def test_markdown_discloses_unattributed_burn(self, result):
        md = render_burn_rate_markdown(result)
        assert "Not governance-attributable: 30 tokens" in md


# ---------------------------------------------------------------------------
# D5 — subagent exclusion disclosure.
# ---------------------------------------------------------------------------


class TestSubagentDisclosure:
    @pytest.fixture
    def result(self, tmp_path):
        sink = tmp_path / "t.jsonl"
        tr = tmp_path / "tr.jsonl"
        tr.write_text(
            "\n".join(json.dumps(o) for o in [
                _assistant("req_A", "toolu_A1", _u(100)),
                _assistant("req_T", "toolu_T1", _u(40)),
            ]) + "\n"
        )
        _run(_pre("Edit", "toolu_A1"), sink)
        _run(_pre("Task", "toolu_T1"), sink)  # subagent spawn
        _run(_session_end(tr), sink)
        return compute_policy_violation_burn_rate(_read(sink))

    def test_field_set(self, result):
        assert result["subagent_activity_present"] is True

    def test_cli_discloses_exclusion(self, result):
        out = render_burn_rate_cli(result, color=False)
        assert "Subagent (Task/Agent) token burn is excluded" in out

    def test_no_disclosure_without_subagent(self, tmp_path):
        sink = tmp_path / "t.jsonl"
        tr = tmp_path / "tr.jsonl"
        tr.write_text(json.dumps(_assistant("req_A", "toolu_A1", _u(100))) + "\n")
        _run(_pre("Edit", "toolu_A1"), sink)
        _run(_session_end(tr), sink)
        result = compute_policy_violation_burn_rate(_read(sink))
        assert result["subagent_activity_present"] is False
        assert "Subagent" not in render_burn_rate_cli(result, color=False)


# ---------------------------------------------------------------------------
# R5 — scale: full SessionEnd -> analyzers pipeline at thousands of turns.
# ---------------------------------------------------------------------------


class TestScaleR5:
    def test_thousands_of_turns_pipeline(self, tmp_path):
        n = 2000
        sink = tmp_path / "t.jsonl"
        tr = tmp_path / "tr.jsonl"
        # One tool call + token turn per request; each burns 10 output tokens.
        with tr.open("w") as fh:
            for i in range(n):
                fh.write(json.dumps(_assistant(f"req_{i}", f"toolu_{i}", _u(10))) + "\n")
        # A couple of live tool calls so there is real attribution to join.
        _run(_pre("Edit", "toolu_0"), sink)
        _run(_pre("Edit", "toolu_1"), sink)
        _run(_session_end(tr), sink)

        events = _read(sink)
        # All n turns emitted (D7: every requestId), plus the live events.
        token_snaps = [
            e for e in events
            if e["event_type"] == "CONTEXT_SNAPSHOT"
            and e["payload"].get("llm_turn_id")
        ]
        assert len(token_snaps) == n

        burn = compute_policy_violation_burn_rate(events)
        assert burn["total_tokens"] == n * 10  # every turn's burn counted once
        # The two live Edits joined to their turns → those rules attribute.
        assert burn["status"] in ("ok", "partial")

        pulse = compute_pulse(events)
        # Pulse composes without error at scale and renders.
        assert isinstance(render_pulse_cli(pulse, color=False), str)
