#!/usr/bin/env python3
"""v0.2.6.1 demo — Claude Code per-turn token-burn capture, end to end.

Shows the headline feature of v0.2.6.1: a Claude Code session now produces
real per-turn token-burn attribution in `sentience pulse`, where v0.2.6
returned `no_signal`.

It synthesizes a realistic, cache-heavy Claude Code session (read pricing →
edit it → grep → spin a subagent → answer), drives it through the actual
Claude Code hook (PreToolUse / PostToolUse + the new SessionEnd handler),
then renders the pulse — all into a temp directory, touching nothing under
your real ~/.sentience.

Run it:

    python examples/v0261_claude_code_token_capture_demo.py

Nothing here is part of the test suite or the shipped runtime — it's a
demonstration artifact only. The same result on a *real* session is:

    sentience init claude-code     # wires PreToolUse / PostToolUse / SessionEnd
    # ...run a Claude Code session, then end it...
    sentience pulse --latest
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sentience_governor.analyze.pulse import compute_pulse
from sentience_governor.analyze.renderers import render_pulse_cli
from sentience_governor.wrapper.claude_code_hook import ClaudeCodeGovernanceHook

SESSION_ID = "demo-pricing-refactor"


def _usage(input_tokens, cache_write, cache_read, output):
    """Anthropic Message.usage shape. Cache dominates Claude Code usage —
    note how tiny `input_tokens` is next to `cache_read_input_tokens`."""
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": cache_write,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
    }


def _assistant(request_id, tool_use_id, tool_name, usage):
    content = []
    if tool_use_id:
        content.append(
            {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": {}}
        )
    return {
        "type": "assistant",
        "requestId": request_id,
        "message": {
            "role": "assistant",
            "model": "claude-opus-4",
            "content": content,
            "usage": usage,
        },
    }


def build_session(workdir: Path) -> Path:
    """Write a synthetic transcript and drive the hook → return the trace path."""
    sink = workdir / "session.jsonl"
    transcript = workdir / "transcript.jsonl"

    # A realistic, cache-heavy session. burn = input + cache_write + cache_read +
    # output (Anthropic is cache-additive). req_5 is a pure answer turn (no tool).
    transcript.write_text(
        "\n".join(
            json.dumps(o)
            for o in [
                _assistant("req_1", "toolu_read1", "Read", _usage(3, 0, 38600, 120)),
                _assistant("req_2", "toolu_edit1", "Edit", _usage(5, 400, 24000, 600)),
                _assistant("req_3", "toolu_bash1", "Bash", _usage(4, 0, 8000, 90)),
                _assistant("req_4", "toolu_task1", "Task", _usage(2, 0, 3000, 40)),
                _assistant("req_5", None, None, _usage(6, 0, 5000, 300)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def run(payload):
        ClaudeCodeGovernanceHook(payload, sink).process()

    # Live tool calls — each carries the tool_use_id the join keys on.
    for tool, tuid in [
        ("Read", "toolu_read1"),
        ("Edit", "toolu_edit1"),
        ("Bash", "toolu_bash1"),
        ("Task", "toolu_task1"),
    ]:
        run({
            "hook_event_name": "PreToolUse", "session_id": SESSION_ID,
            "tool_name": tool, "tool_input": {"file_path": "pricing.md"},
            "tool_use_id": tuid,
        })
        run({
            "hook_event_name": "PostToolUse", "session_id": SESSION_ID,
            "tool_name": tool, "tool_input": {"file_path": "pricing.md"},
            "tool_response": {"ok": True}, "tool_use_id": tuid,
        })

    # Session ends → the v0.2.6.1 SessionEnd handler parses the transcript and
    # appends one token-bearing CONTEXT_SNAPSHOT per model turn (requestId).
    run({
        "hook_event_name": "SessionEnd", "session_id": SESSION_ID,
        "transcript_path": str(transcript),
    })
    return sink


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sentience-demo-") as tmp:
        trace_path = build_session(Path(tmp))
        events = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        pulse = compute_pulse(events)

        print("=" * 70)
        print("v0.2.6.1 — Claude Code per-turn token-burn capture")
        print("=" * 70)
        print(
            "\nBefore v0.2.6.1, this same session rendered `no_signal` — the hook\n"
            "captured tool calls but no per-turn token burn. Now:\n"
        )
        print(render_pulse_cli(pulse, color=False))
        print(
            "What to point at:\n"
            "  - Burn is cache-dominated: a turn shows ~3 input tokens but 38,600\n"
            "    cached-read. Counting only input would report ~20 tokens total.\n"
            "  - The join is exact: POL-004 lands on ONE turn (the Edit/memory\n"
            "    write), not smeared across the session.\n"
            "  - Total vs governance-attributable burn is distinguished; the\n"
            "    answer turn's burn is disclosed as not-attributed, not dropped.\n"
            "  - Subagent (Task) burn is excluded and the report says so.\n"
        )


if __name__ == "__main__":
    main()
