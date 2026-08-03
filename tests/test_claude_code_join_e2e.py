"""CP4a — end-to-end tool_use_id join attribution (v0.2.6.1).

Builds a REAL Claude Code trace by driving the hook (CP2/CP3 output), then
runs the burn-rate + undeclared-intent analyzers over it and asserts the
join attributes each tool's violations to its OWN model turn — not all to
the first turn (which is what the legacy positional bracketing would do,
since the token-bearing snapshots arrive at the very end of the trace).

The discriminating assertion is turn_count == 2 for the shared rules:
positional mis-attribution would give 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sentience_governor.analyze.policy_violation_burn_rate import (
    compute_policy_violation_burn_rate,
)
from sentience_governor.analyze.undeclared_intent import (
    compute_undeclared_intent_spend,
)
from sentience_governor.wrapper.claude_code_hook import ClaudeCodeGovernanceHook

SESSION = "sess-join-e2e"

# Distinct per-turn burns so attribution is exactly verifiable.
BURN_A = 2 + 330 + 38631 + 319  # req_A: 39282
BURN_B = 10 + 0 + 1000 + 50  # req_B: 1060


def _run(payload: dict, sink: Path) -> None:
    ClaudeCodeGovernanceHook(payload, sink).process()


def _read_events(sink: Path) -> List[dict]:
    return [
        json.loads(l)
        for l in sink.read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]


def _pre(tool, tool_use_id, **inp):
    return {
        "hook_event_name": "PreToolUse",
        "session_id": SESSION,
        "tool_name": tool,
        "tool_input": inp or {"file_path": "/x"},
        "tool_use_id": tool_use_id,
    }


def _post(tool, tool_use_id):
    return {
        "hook_event_name": "PostToolUse",
        "session_id": SESSION,
        "tool_name": tool,
        "tool_input": {"file_path": "/x"},
        "tool_response": {"ok": True},
        "tool_use_id": tool_use_id,
    }


def _assistant(request_id, tool_id, usage):
    return {
        "type": "assistant",
        "requestId": request_id,
        "message": {
            "role": "assistant",
            "model": "claude-anon",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Edit", "input": {}}],
            "usage": usage,
        },
    }


def _usage(i, cw, cr, o):
    return {
        "input_tokens": i,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "output_tokens": o,
    }


@pytest.fixture
def trace(tmp_path: Path) -> List[dict]:
    """Drive the hook to produce a real two-turn Claude Code trace."""
    sink = tmp_path / "trace.jsonl"
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            json.dumps(o)
            for o in [
                _assistant("req_A", "toolu_A1", _usage(2, 330, 38631, 319)),
                _assistant("req_B", "toolu_B1", _usage(10, 0, 1000, 50)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # Two Edit tool calls (WRITE + persistence) → each fires POL-001 (no
    # declared intent), POL-003 (unclassified context), POL-004 (memory write).
    _run(_pre("Edit", "toolu_A1"), sink)
    _run(_post("Edit", "toolu_A1"), sink)
    _run(_pre("Edit", "toolu_B1"), sink)
    _run(_post("Edit", "toolu_B1"), sink)
    _run(
        {
            "hook_event_name": "SessionEnd",
            "session_id": SESSION,
            "transcript_path": str(transcript),
        },
        sink,
    )
    return _read_events(sink)


# ---------------------------------------------------------------------------
# Burn-rate join.
# ---------------------------------------------------------------------------


class TestBurnRateJoin:
    def test_both_turns_have_tokens(self, trace):
        result = compute_policy_violation_burn_rate(trace)
        assert result["status"] == "ok"
        # Two turns, distinct burns; total is their sum.
        assert result["total_tokens"] == BURN_A + BURN_B

    def test_shared_rule_attributes_to_BOTH_turns(self, trace):
        # The discriminator: positional bracketing would dump every violation
        # on the first turn (turn_count == 1). The join spreads them correctly.
        result = compute_policy_violation_burn_rate(trace)
        for rule in ("POL-001", "POL-003", "POL-004"):
            assert rule in result["by_rule"], rule
            slot = result["by_rule"][rule]
            assert slot["turn_count"] == 2, f"{rule} mis-attributed"
            assert slot["token_cost"] == BURN_A + BURN_B, rule
            assert set(slot["sample_turn_ids"]) == {"req_A", "req_B"}, rule

    def test_violation_associated_equals_total(self, trace):
        result = compute_policy_violation_burn_rate(trace)
        # Both turns fired violations → all burn is governance-attributable.
        assert result["violation_associated_tokens"] == BURN_A + BURN_B
        assert result["violation_firing_turns"] == 2


# ---------------------------------------------------------------------------
# Undeclared-intent join.
# ---------------------------------------------------------------------------


class TestUndeclaredIntentJoin:
    def test_both_turns_counted_undeclared(self, trace):
        result = compute_undeclared_intent_spend(trace)
        # POL-001 fired on both tools → both turns are undeclared.
        assert result["undeclared_turn_count"] == 2
        assert result["undeclared_tokens"] == BURN_A + BURN_B
        ids = {t["turn_id"] for t in result["undeclared_turns"]}
        assert ids == {"req_A", "req_B"}

    def test_per_turn_tokens_correct(self, trace):
        result = compute_undeclared_intent_spend(trace)
        by_id = {t["turn_id"]: t["tokens"] for t in result["undeclared_turns"]}
        assert by_id["req_A"] == BURN_A
        assert by_id["req_B"] == BURN_B
