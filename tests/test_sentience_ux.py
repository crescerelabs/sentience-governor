"""Acceptance tests for the `sentience` CLI (agent-hook viewer).

Covers the 15 CLI UX acceptance cases
plus additional coverage of the architect checklist (§12 Trust &
correctness gates).

Tests use in-file event factories rather than separate JSONL fixture
files. Each test is self-documenting about the trace shape it needs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterable, List, Tuple

import pytest

from sentience_governor.cli.ux import (
    _BASELINE_FREQUENCY_THRESHOLD,
    _EventAnomaly,
    _format_anomaly_action,
    _format_event_oneline,
    _format_tool_action,
    _gloss,
    _latest_token_bearing_session,
    classify_session,
    render_open,
    run_list,
    run_open,
    run_pulse,
    run_status,
)


# ---------------------------------------------------------------------------
# Event factories — keep tests readable
# ---------------------------------------------------------------------------


def _agent_registered(seq: int = 1, session_id: str = "sess-test") -> dict:
    return {
        "event_id": f"evt-reg-{seq}",
        "event_type": "AGENT_REGISTERED",
        "session_id": session_id,
        "event_sequence_number": seq,
        "previous_event_id": None,
        "agent_id": "claude-code-test",
        "deployment_mode": "vendor_managed",
        "timestamp_utc": "2026-04-20T10:00:00.000Z",
        "primitive": "REGISTRATION",
        "payload": {
            "agent_id": "claude-code-test",
            "agent_version": "0.1.9",
            "deployment_mode": "vendor_managed",
            "declared_capabilities": [],
        },
        "advisory_flags": [],
        "policy_violations": [],
    }


def _intent_declared(
    seq: int = 2,
    session_id: str = "sess-test",
    source: str = "none",
    with_missing_flag: bool = True,
) -> dict:
    flags = ["INTENT_MISSING"] if with_missing_flag else []
    return {
        "event_id": f"evt-intent-{seq}",
        "event_type": "INTENT_DECLARED",
        "session_id": session_id,
        "event_sequence_number": seq,
        "agent_id": "claude-code-test",
        "deployment_mode": "vendor_managed",
        "timestamp_utc": "2026-04-20T10:00:01.000Z",
        "primitive": "INTENT",
        "payload": {
            "intent_source": source,
            "intent_confidence": "unknown" if source == "none" else "explicit",
        },
        "advisory_flags": flags,
        "policy_violations": [],
    }


def _scope_asserted(
    seq: int,
    tool_id: str,
    operation_type: str = "READ",
    target_system: str = "filesystem",
    session_id: str = "sess-test",
    flags: Iterable[str] = (),
    violations: Iterable[str] = (),
) -> dict:
    return {
        "event_id": f"evt-scope-{seq}",
        "event_type": "SCOPE_ASSERTED",
        "session_id": session_id,
        "event_sequence_number": seq,
        "agent_id": "claude-code-test",
        "deployment_mode": "vendor_managed",
        "timestamp_utc": "2026-04-20T10:00:02.000Z",
        "primitive": "SCOPE",
        "payload": {
            "tool_id": tool_id,
            "operation_type": operation_type,
            "target_system": target_system,
            "asserted_permissions": [operation_type.lower()],
        },
        "advisory_flags": list(flags),
        "policy_violations": list(violations),
    }


def _context_snapshot(
    seq: int,
    session_id: str = "sess-test",
    classified: bool = False,
    tokens: int = 100,
) -> dict:
    if classified:
        flags: List[str] = []
        vios: List[str] = []
        source = "integrator_declared"
    else:
        flags = ["CONTEXT_UNCLASSIFIED"]
        vios = ["POL-003"]
        source = "unclassified"
    return {
        "event_id": f"evt-ctx-{seq}",
        "event_type": "CONTEXT_SNAPSHOT",
        "session_id": session_id,
        "event_sequence_number": seq,
        "agent_id": "claude-code-test",
        "deployment_mode": "vendor_managed",
        "timestamp_utc": "2026-04-20T10:00:03.000Z",
        "primitive": "CONTEXT",
        "payload": {
            "classification_source": source,
            "context_size_tokens": tokens,
            "data_classifications": [] if not classified else ["internal"],
        },
        "advisory_flags": flags,
        "policy_violations": vios,
    }


def _memory_write(
    seq: int, session_id: str = "sess-test", target: str = "filesystem"
) -> dict:
    return {
        "event_id": f"evt-mem-{seq}",
        "event_type": "MEMORY_WRITE_ATTEMPT",
        "session_id": session_id,
        "event_sequence_number": seq,
        "agent_id": "claude-code-test",
        "deployment_mode": "vendor_managed",
        "timestamp_utc": "2026-04-20T10:00:04.000Z",
        "primitive": "MEMORY",
        "payload": {
            "target_store": target,
            "write_type": "write_to_persistence_target",
            "write_classification": "unclassified",
            "detection_mechanism": "tool_metadata",
        },
        "advisory_flags": ["MEMORY_WRITE_CANDIDATE"],
        "policy_violations": ["POL-004"],
    }


def _ctx_with_turn(
    seq: int, session_id: str, turn_id: str, tokens: int = 100
) -> dict:
    """A CONTEXT_SNAPSHOT carrying a per-turn token id — i.e. token-bearing."""
    ev = _context_snapshot(seq, session_id=session_id, classified=True, tokens=tokens)
    ev["payload"]["llm_turn_id"] = turn_id
    ev["payload"]["llm_prompt_tokens"] = tokens
    ev["payload"]["llm_completion_tokens"] = 0
    return ev


def _write_trace(path: Path, events: List[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _make_session_file(
    tmp_path: Path, session_id: str, events: List[dict]
) -> Path:
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    f = trace_dir / f"{session_id}.jsonl"
    _write_trace(f, events)
    return f


# ---------------------------------------------------------------------------
# Baseline-noise classifier
# ---------------------------------------------------------------------------


class TestBaselineClassifier:
    def test_all_baseline_noise_classifies_as_zero_anomalies(self):
        # 1 INTENT_MISSING (1/1 = 100%), 10 POL-003 (10/10 = 100%).
        # Both patterns cross 80% → both are baseline → zero anomalies.
        events = [
            _agent_registered(1),
            _intent_declared(2),
        ]
        for i in range(10):
            events.append(_context_snapshot(3 + i))
        result = classify_session(events)
        assert result.anomalies == []
        assert "INTENT_MISSING" in result.baseline_codes_present
        assert "POL-003" in result.baseline_codes_present or \
               "CONTEXT_UNCLASSIFIED" in result.baseline_codes_present

    def test_frequency_threshold_below_80_surfaces_anomaly(self):
        # 3 of 10 CONTEXT_SNAPSHOT events unclassified (30% — below 80%).
        # POL-003 should NOT be baseline; those 3 events are anomalies.
        events = [_agent_registered(1), _intent_declared(2)]
        # 7 classified, 3 unclassified
        for i in range(7):
            events.append(_context_snapshot(3 + i, classified=True))
        for i in range(3):
            events.append(_context_snapshot(10 + i, classified=False))
        result = classify_session(events)
        # 3 anomalies expected (from unclassified CONTEXT_SNAPSHOTs).
        assert len(result.anomalies) == 3
        assert "POL-003" in result.anomaly_code_counts or \
               "CONTEXT_UNCLASSIFIED" in result.anomaly_code_counts

    def test_anomaly_with_real_violation_surfaces(self):
        # One SCOPE_INTENT_MISMATCH inside otherwise-baseline session.
        events = [
            _agent_registered(1),
            _intent_declared(2),
            _scope_asserted(
                3, "Edit",
                flags=["SCOPE_INTENT_MISMATCH"],
                violations=["POL-001"],
            ),
            _context_snapshot(4),
        ]
        result = classify_session(events)
        # Two anomalies on event 3: SCOPE_INTENT_MISMATCH (advisory)
        # and POL-001 (violation). The single event produces one
        # _EventAnomaly entry with the higher-priority primary code.
        assert len(result.anomalies) == 1
        assert result.anomalies[0].sequence == 3
        # Both codes should appear in all_codes.
        assert "SCOPE_INTENT_MISMATCH" in result.anomalies[0].all_codes


# ---------------------------------------------------------------------------
# Event formatter — locked rules per §5.6
# ---------------------------------------------------------------------------


class TestEventFormatting:
    @pytest.mark.parametrize("tool,inp,expected_contains", [
        ("Bash", {"command": "ls -la"}, 'Bash → run("ls -la")'),
        ("Edit", {"file_path": "/x.py"}, 'Edit → "/x.py"'),
        ("Write", {"file_path": "/y.py"}, 'Write → "/y.py"'),
        ("Read", {"file_path": "/z.py"}, 'Read → "/z.py"'),
        ("Grep", {"pattern": "TODO", "path": "/src"}, 'Grep → "TODO" in "/src"'),
        ("Glob", {"pattern": "**/*.py"}, 'Glob → "**/*.py"'),
        ("WebFetch", {"url": "https://example.com"},
            'WebFetch → https://example.com'),
        ("WebSearch", {"query": "sentience governor"},
            'WebSearch → "sentience governor"'),
    ])
    def test_builtin_tool_formatting(self, tool, inp, expected_contains):
        assert _format_tool_action(tool, inp) == expected_contains

    def test_bash_command_truncated_at_60_chars(self):
        cmd = "x" * 100
        out = _format_tool_action("Bash", {"command": cmd})
        # Rendered form includes ellipsis marker and no full command.
        assert "…" in out
        # Shown portion is the first 60 chars of the command.
        assert f'"{"x" * 60}…"' in out

    def test_mcp_tool_renders_as_bracketed_provider(self):
        assert (
            _format_tool_action("mcp__airtable__search_records", {})
            == "MCP[airtable] → search_records(...)"
        )

    def test_unknown_tool_preserves_identity_degrades_payload(self):
        # F-V3: unknown tool with no meaningful target renders as a clean
        # bare label — no broken-looking "→ ???".
        assert _format_tool_action("FutureCursorTool", {}) == "FutureCursorTool"

    def test_bash_with_missing_payload_preserves_identity(self):
        # F-V3: tool identity survives an empty payload; no "→ ???".
        assert _format_tool_action("Bash", {}) == "Bash"

    def test_toolsearch_renders_clean_label(self):
        # F-V3: ToolSearch (no query field in its payload) renders bare,
        # never "ToolSearch → ???".
        assert _format_tool_action("ToolSearch", {}) == "ToolSearch"
        # If a query ever appears, it is shown.
        assert (
            _format_tool_action("ToolSearch", {"query": "find_x"})
            == 'ToolSearch → "find_x"'
        )

    def test_non_tool_event_reads_as_narrative(self):
        intent = _intent_declared(1, source="none")
        assert _format_event_oneline(intent) == "INTENT_DECLARED → none provided"

        ctx = _context_snapshot(2, classified=False, tokens=44)
        assert (
            _format_event_oneline(ctx)
            == "CONTEXT_SNAPSHOT → unclassified context (44 tokens)"
        )

        mem = _memory_write(3, target="database")
        assert _format_event_oneline(mem) == "MEMORY_WRITE_ATTEMPT → write to database"

    def test_truly_unknown_tool_name_does_not_crash(self):
        # Empty tool name (edge case): safe, non-broken-looking sentinel.
        assert _format_tool_action("", {}) == "(unknown tool)"


# ---------------------------------------------------------------------------
# Gloss table
# ---------------------------------------------------------------------------


class TestGloss:
    def test_known_codes_return_gloss(self):
        assert _gloss("POL-001") == "write operation without declared intent"
        assert _gloss("MEMORY_WRITE_CANDIDATE") == "write outside declared scope"
        assert _gloss("SCOPE_INTENT_MISMATCH") == (
            "tool call does not match declared intent"
        )

    def test_unknown_code_returns_empty_string(self):
        # Per plan: no invented wording for unknown codes.
        assert _gloss("POL-999") == ""
        assert _gloss("NOVEL_FLAG") == ""


# ---------------------------------------------------------------------------
# Render — full session
# ---------------------------------------------------------------------------


class TestRenderOpen:
    def test_clean_session_produces_positive_signal(self, tmp_path: Path):
        events = [
            _agent_registered(1),
            _intent_declared(2),
            _context_snapshot(3),
            _context_snapshot(4),
            _context_snapshot(5),
        ]
        f = _make_session_file(tmp_path, "sess-clean", events)
        out = render_open("sess-clean", events, f)

        # Summary: ✓ baseline behavior, no violations count
        assert "Status: ✓ baseline behavior" in out
        # Focus: positive signal wording
        assert "No anomalies detected" in out
        assert "expected Claude Code baseline behavior" in out
        # Notes block present (baseline noise IS present)
        assert "INTENT_MISSING is expected" in out
        assert "POL-003 appears on most context reads" in out
        # Key Events: clean
        assert "No violations detected in this session" in out

    def test_anomalous_session_produces_summary_focus_key_events(
        self, tmp_path: Path
    ):
        events = [
            _agent_registered(1),
            _intent_declared(2),
            _context_snapshot(3),
            _scope_asserted(
                4, "Edit", operation_type="WRITE",
                flags=["SCOPE_INTENT_MISMATCH"], violations=["POL-001"],
            ),
            _memory_write(5),
            _context_snapshot(6),
        ]
        f = _make_session_file(tmp_path, "sess-anomaly", events)
        out = render_open("sess-anomaly", events, f)

        assert "Status: ⚠ anomalies detected" in out
        # Summary splits policy violations from advisory flags (FIX-3
        # propagation, F19(b)) — never a conflated "Violations" count.
        # POL-001 (scope) + POL-004 (memory write) = 2 policy;
        # SCOPE_INTENT_MISMATCH + MEMORY_WRITE_CANDIDATE = 2 advisory.
        assert "⚠ Policy violations: 2" in out
        assert "Advisory flags:    2" in out
        assert "⚠ Violations:" not in out
        # F19(a): the tool list is self-labeled so a reader knows it is
        # tool-call frequency (SCOPE_ASSERTED), not the event total. One
        # SCOPE_ASSERTED (Edit) in this session.
        assert "Tool calls observed: 1" in out
        assert "Top tools by SCOPE_ASSERTED count:" in out
        assert "Edit (1)" in out
        # The bare, ambiguous "Top tools:" header is gone.
        assert "Top tools:\n" not in out
        # Focus mentions specific anomaly groups with gloss-like wording
        assert "Focus (what to pay attention to)" in out
        assert "⚠" in out  # bullet severity marker
        # Key Events show with inline sequence brackets and gloss
        assert "⚠ [" in out
        assert "(write outside declared scope)" in out
        # Footer present
        assert "sentience list" in out
        assert "after running Claude Code" in out

    def test_summary_and_focus_are_derived_from_same_source(
        self, tmp_path: Path
    ):
        # Architect §5.2 coupling rule: Summary's anomaly counts and
        # Focus bullets must refer to the same underlying events. Test
        # invariant: Focus bullets never outnumber Summary-reported
        # anomaly events.
        events = [
            _agent_registered(1),
            _intent_declared(2),
            _scope_asserted(
                3, "Edit", operation_type="WRITE",
                flags=["SCOPE_INTENT_MISMATCH"], violations=["POL-001"],
            ),
            _scope_asserted(
                4, "Bash", operation_type="EXECUTE",
                violations=["POL-001"],
            ),
            _memory_write(5),
        ]
        result = classify_session(events)
        # Total anomalies (by event) must equal or exceed all focus bullet totals
        total_focus_events = sum(
            int(b.description.split()[0])
            for b in __import__(
                "sentience_governor.cli.ux", fromlist=["_build_focus_bullets"]
            )._build_focus_bullets(result)
        )
        # Each anomaly event is represented in at most one focus bullet
        assert total_focus_events == len(result.anomalies)


# ---------------------------------------------------------------------------
# `sentience status` command
# ---------------------------------------------------------------------------


class TestStatus:
    def test_missing_trace_dir_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv(
            "SENTIENCE_CLAUDE_CODE_SINK_PATH", str(tmp_path / "nope")
        )
        code = run_status(argparse_ns())
        out = capsys.readouterr().out
        assert code == 1
        assert "Hook:           not detected" in out
        assert "then run:" in out

    def test_empty_dir_exits_1_with_trace_path_available(
        self, tmp_path, monkeypatch, capsys
    ):
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        code = run_status(argparse_ns())
        out = capsys.readouterr().out
        assert code == 1
        assert "Hook:           trace path available" in out
        assert "No sessions captured yet." in out

    def test_missing_trace_dir_json_emits_json(
        self, tmp_path, monkeypatch, capsys
    ):
        """KG-1 (v0.2.8.1): --json must emit JSON (not human text) on the
        no-trace-dir early return, preserving exit code 1."""
        monkeypatch.setenv(
            "SENTIENCE_CLAUDE_CODE_SINK_PATH", str(tmp_path / "nope")
        )
        code = run_status(argparse_ns(json=True))
        out = capsys.readouterr().out
        assert code == 1
        data = json.loads(out)  # must be valid JSON, not human text
        assert data["hook"] == "not detected"
        assert data["last_session"] is None
        assert "trace_path" in data
        assert "Sentience Status" not in out
        assert "then run:" not in out

    def test_empty_dir_json_emits_json(
        self, tmp_path, monkeypatch, capsys
    ):
        """KG-1 (v0.2.8.1): --json on the no-sessions-captured early return."""
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        code = run_status(argparse_ns(json=True))
        out = capsys.readouterr().out
        assert code == 1
        data = json.loads(out)
        assert data["hook"] == "trace path available"
        assert data["last_session"] is None
        assert "No sessions captured yet." not in out

    def test_populated_dir_exits_0_with_sessions_detected(
        self, tmp_path, monkeypatch, capsys
    ):
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        events = [
            _agent_registered(1),
            _intent_declared(2),
            _scope_asserted(3, "Read", session_id="sess-live"),
            _context_snapshot(4),
        ]
        _write_trace(trace_dir / "sess-live.jsonl", events)
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        code = run_status(argparse_ns())
        out = capsys.readouterr().out
        assert code == 0
        assert "Hook:           sessions detected" in out
        assert "Sentience is governing your Claude Code sessions locally." in out
        # Hook line has no emoji (semantic label only)
        hook_line = [line for line in out.splitlines()
                     if line.startswith("Hook:")][0]
        assert "✅" not in hook_line
        assert "⚠" not in hook_line

    def test_status_splits_violations_from_advisory_flags(
        self, tmp_path, monkeypatch, capsys
    ):
        """FIX-3 (v0.2.8): never an advisory count under a 'Violations'
        label. Advisory-only anomalies must show Policy violations: 0."""
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        # One advisory-flag anomaly (no POL code), zero policy violations.
        events = [
            _agent_registered(1),
            _scope_asserted(
                2, "Bash", session_id="sess-adv",
                flags=["SCOPE_OPERATION_UNEXPECTED"], violations=[],
            ),
        ]
        _write_trace(trace_dir / "sess-adv.jsonl", events)
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        code = run_status(argparse_ns())
        out = capsys.readouterr().out
        assert code == 0
        assert "Policy violations:  0" in out
        assert "Advisory flags:     1" in out
        # The old conflated label must be gone.
        assert "Violations:   " not in out

    def test_status_json_reconciles_raw_vs_displayed(
        self, tmp_path, monkeypatch, capsys
    ):
        """FIX-3 hard requirement: `status --json` exposes the fields to
        reconcile raw vs displayed counts (raw = pol + adv + baseline)."""
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        # 5 unclassified snapshots -> (CONTEXT_SNAPSHOT, POL-003) and
        # CONTEXT_UNCLASSIFIED are >80% frequent -> baseline-filtered.
        # 1 violation-bearing scope + 1 advisory-bearing scope -> anomalies.
        events = [_agent_registered(1)]
        events += [
            _context_snapshot(i, session_id="sess-json") for i in range(2, 7)
        ]
        events.append(
            _scope_asserted(
                7, "Bash", session_id="sess-json",
                flags=["SCOPE_INTENT_MISMATCH"], violations=["POL-001"],
            )
        )
        _write_trace(trace_dir / "sess-json.jsonl", events)
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        code = run_status(argparse_ns(json=True))
        out = capsys.readouterr().out
        assert code == 0
        data = json.loads(out)
        last = data["last_session"]
        assert last["raw_total"] == (
            last["policy_violations"]
            + last["advisory_flags"]
            + last["baseline_filtered_total"]
        )
        # The baseline-filtered codes are named with counts.
        assert last["baseline_filtered"].get("POL-003") == 5
        assert last["baseline_filtered"].get("CONTEXT_UNCLASSIFIED") == 5
        assert last["policy_violations"] == 1   # POL-001
        assert last["advisory_flags"] == 1      # SCOPE_INTENT_MISMATCH


# ---------------------------------------------------------------------------
# `sentience list` command
# ---------------------------------------------------------------------------


class TestList:
    def test_empty_dir_exits_0_with_consistent_wording(
        self, tmp_path, monkeypatch, capsys
    ):
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        code = run_list(argparse_ns())
        out = capsys.readouterr().out
        assert code == 0
        # Same wording as sentience status empty state.
        assert "No sessions found yet." in out
        assert "then run:" in out

    def test_lists_sessions_newest_first(self, tmp_path, monkeypatch, capsys):
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        # Three sessions, written oldest → newest.
        for sid, delay in [("sess-old", 0), ("sess-mid", 0.01), ("sess-new", 0.02)]:
            events = [_agent_registered(1, session_id=sid),
                      _intent_declared(2, session_id=sid)]
            f = trace_dir / f"{sid}.jsonl"
            _write_trace(f, events)
            import time as _t
            _t.sleep(delay)
            os.utime(f, None)
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        code = run_list(argparse_ns())
        out = capsys.readouterr().out
        assert code == 0
        # Newest first — sess-new should appear before sess-old.
        assert out.index("sess-new") < out.index("sess-old")


# ---------------------------------------------------------------------------
# `sentience open` command
# ---------------------------------------------------------------------------


class TestOpen:
    def test_open_latest(self, tmp_path, monkeypatch, capsys):
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        events = [
            _agent_registered(1, session_id="sess-live-abc123"),
            _intent_declared(2, session_id="sess-live-abc123"),
            _scope_asserted(3, "Edit", session_id="sess-live-abc123"),
            _context_snapshot(4, session_id="sess-live-abc123"),
        ]
        _write_trace(trace_dir / "sess-live-abc123.jsonl", events)
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        ns = argparse_ns(latest=True, session_id=None)
        code = run_open(ns)
        out = capsys.readouterr().out
        assert code == 0
        # Header + Summary + Focus + Full Trace blocks must all appear.
        assert "Session: sess-live-abc123" in out
        assert "Summary" in out
        assert "Focus (what to pay attention to)" in out
        assert "Full Trace" in out

    def test_open_by_exact_session_id(self, tmp_path, monkeypatch, capsys):
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        for sid in ("sess-one", "sess-two"):
            events = [_agent_registered(1, session_id=sid),
                      _intent_declared(2, session_id=sid)]
            _write_trace(trace_dir / f"{sid}.jsonl", events)
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        ns = argparse_ns(latest=False, session_id="sess-two")
        code = run_open(ns)
        out = capsys.readouterr().out
        assert code == 0
        assert "Session: sess-two" in out

    def test_open_missing_session_returns_1(
        self, tmp_path, monkeypatch, capsys
    ):
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        _write_trace(
            trace_dir / "sess-one.jsonl",
            [_agent_registered(1, session_id="sess-one")],
        )
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
        ns = argparse_ns(latest=False, session_id="sess-nope")
        code = run_open(ns)
        err = capsys.readouterr().err
        assert code == 1
        assert "No session matching" in err

    def test_open_summary_flag_omits_full_trace(
        self, tmp_path, monkeypatch, capsys
    ):
        """--summary skips the Full Trace block for terminal readability.

        Every other block (Header, Summary, Focus, Notes, Key Events,
        Footer) must still appear. The JSONL file on disk is
        unchanged. Default behaviour (no --summary) still shows Full
        Trace — tested via the existing test_open_latest check.
        """
        trace_dir = tmp_path / "tr"
        trace_dir.mkdir()
        events = [
            _agent_registered(1, session_id="sess-summary-abc"),
            _intent_declared(2, session_id="sess-summary-abc"),
            _scope_asserted(3, "Edit", session_id="sess-summary-abc"),
            _context_snapshot(4, session_id="sess-summary-abc"),
            _memory_write(5, session_id="sess-summary-abc"),
        ]
        _write_trace(trace_dir / "sess-summary-abc.jsonl", events)
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))

        # With --summary: every block except Full Trace.
        ns = argparse_ns(latest=True, session_id=None, summary=True)
        code = run_open(ns)
        out = capsys.readouterr().out
        assert code == 0
        assert "Session: sess-summary-abc" in out
        assert "Summary" in out
        assert "Focus (what to pay attention to)" in out
        assert "Key Events" in out
        assert "Next steps" in out  # Footer present
        # Full Trace header MUST NOT appear.
        assert "Full Trace" not in out

        # Without --summary: Full Trace appears as usual.
        capsys.readouterr()  # clear
        ns_default = argparse_ns(latest=True, session_id=None, summary=False)
        code = run_open(ns_default)
        out_default = capsys.readouterr().out
        assert code == 0
        assert "Full Trace" in out_default

    def test_render_open_summary_parameter(self, tmp_path):
        """Unit-level: render_open(summary=True) skips Full Trace block."""
        events = [
            _agent_registered(1),
            _intent_declared(2),
            _scope_asserted(3, "Read"),
            _context_snapshot(4),
        ]
        f = _make_session_file(tmp_path, "sess-unit", events)
        rendered_full = render_open("sess-unit", events, f, summary=False)
        rendered_short = render_open("sess-unit", events, f, summary=True)
        assert "Full Trace" in rendered_full
        assert "Full Trace" not in rendered_short
        # Invariant: both always end with the Footer.
        assert "Next steps" in rendered_full
        assert "Next steps" in rendered_short


# ---------------------------------------------------------------------------
# Golden-trace regression guard — sentience-cli must still work
# ---------------------------------------------------------------------------


class TestSentienceCliUnchanged:
    def test_sentience_cli_module_still_exports_main(self):
        # The existing `sentience-cli` CLI must still be importable and
        # have an unchanged entry point. Golden-trace snapshot tests in
        # tests/test_cli.py are the primary regression guard; this is
        # a belt-and-suspenders import check.
        from sentience_governor.cli import viewer as _viewer
        assert callable(_viewer.main)
        # parse_events is the shared primitive both CLIs use.
        assert callable(_viewer.parse_events)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def argparse_ns(**kwargs):
    """Build a minimal argparse.Namespace for subcommand tests."""
    import argparse
    defaults = {"latest": False, "session_id": None, "summary": False}
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestF18TokenBearingHint:
    """F18 (v0.2.8.1): empty-state reports name the most recent token-bearing
    session so desktop session churn doesn't hide it. Read-only — no reroute."""

    def _rich(self, sid: str) -> List[dict]:
        return [
            _agent_registered(1, session_id=sid),
            _intent_declared(2, session_id=sid),
            _scope_asserted(3, "Read", session_id=sid),
            _ctx_with_turn(4, sid, "turn-1"),
            _ctx_with_turn(5, sid, "turn-2"),
        ]

    def test_helper_finds_token_bearing_session_with_turn_count(self, tmp_path):
        _write_trace(tmp_path / "sess-rich.jsonl", self._rich("sess-rich"))
        _write_trace(
            tmp_path / "sess-live.jsonl",
            [_agent_registered(1, session_id="sess-live")],
        )
        found = _latest_token_bearing_session(
            tmp_path, exclude_session_id="sess-live"
        )
        assert found is not None
        sid, n = found
        assert sid == "sess-rich"
        assert n == 2  # two distinct llm_turn_ids

    def test_helper_excludes_current_session(self, tmp_path):
        # Only the current (rich) session exists → nothing else to point at.
        _write_trace(tmp_path / "sess-rich.jsonl", self._rich("sess-rich"))
        found = _latest_token_bearing_session(
            tmp_path, exclude_session_id="sess-rich"
        )
        assert found is None

    def test_helper_none_when_no_token_data_anywhere(self, tmp_path):
        _write_trace(
            tmp_path / "sess-empty.jsonl",
            [_agent_registered(1, session_id="sess-empty")],
        )
        found = _latest_token_bearing_session(
            tmp_path, exclude_session_id="other"
        )
        assert found is None

    def _live(self, sid: str) -> List[dict]:
        # An empty live segment: events but no token-bearing turns.
        return [
            _agent_registered(1, session_id=sid),
            _scope_asserted(2, "Read", session_id=sid),
            _context_snapshot(3, session_id=sid),  # no llm_turn_id
        ]

    def test_pulse_empty_latest_autoshows_token_bearing_session_F20(
        self, tmp_path, monkeypatch, capsys
    ):
        """F20 (v0.2.8.2): on implicit --latest, an empty live session
        auto-shows the most recent token-bearing session, with a header."""
        _write_trace(tmp_path / "sess-rich.jsonl", self._rich("sess-rich"))
        # Written second → newer → "latest"; no token-bearing turns.
        _write_trace(tmp_path / "sess-live.jsonl", self._live("sess-live"))
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(tmp_path))
        code = run_pulse(argparse_ns(latest=True, no_prompt=True))
        out = capsys.readouterr().out
        assert code == 0
        # Transparent header naming the session it switched to...
        assert "Showing the most recent session that does — sess-rich" in out
        # ...the rich session's pulse actually rendered (token classes)...
        assert "Token classes" in out
        # ...not left in the empty state.
        assert "No usable analyzer signal" not in out

    def test_pulse_explicit_empty_target_honoured_no_autoswitch_F20(
        self, tmp_path, monkeypatch, capsys
    ):
        """F20 escape hatch: an explicit target is never auto-switched."""
        _write_trace(tmp_path / "sess-rich.jsonl", self._rich("sess-rich"))
        _write_trace(tmp_path / "sess-live.jsonl", self._live("sess-live"))
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(tmp_path))
        code = run_pulse(argparse_ns(
            target=str(tmp_path / "sess-live.jsonl"), no_prompt=True))
        out = capsys.readouterr().out
        assert code == 0
        assert "No usable analyzer signal" in out           # honoured exactly
        assert "Showing the most recent session that does" not in out
        assert "Latest session with token data: sess-rich" in out  # F18 hint

    def test_pulse_no_token_bearing_anywhere_stays_empty_F20(
        self, tmp_path, monkeypatch, capsys
    ):
        """F20: with no token-bearing session anywhere, stays empty (no header)."""
        _write_trace(tmp_path / "sess-live.jsonl", self._live("sess-live"))
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(tmp_path))
        code = run_pulse(argparse_ns(latest=True, no_prompt=True))
        out = capsys.readouterr().out
        assert code == 0
        assert "No usable analyzer signal" in out
        assert "Showing the most recent session that does" not in out
