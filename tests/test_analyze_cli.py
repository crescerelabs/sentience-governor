"""Tests for the `sentience analyze undeclared-intent` CLI surface.

Covers:
  - Renderers (cli + markdown) for each status branch
  - --json mode emits the analyzer dict verbatim
  - --no-prompt suppresses any interactive prompt
  - --save writes a report file under a custom reports dir
  - Save flow is suppressed for non-ok status (P7 gate)
  - File-path target mode
  - Session-id-prefix target mode
  - Canonical footer copy is present in saved markdown
  - Differentiated footer copy (agent-bound vs surface-bound)
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

from sentience_governor.analyze import (
    compute_undeclared_intent_spend,
    render_cli,
    render_markdown_report,
)
from sentience_governor.cli import ux as cli_ux


# ---------------------------------------------------------------------------
# Event factories
# ---------------------------------------------------------------------------


def _intent_declared(session_id: str = "sess-1", objective: str = "do x") -> Dict:
    return {
        "event_type": "INTENT_DECLARED",
        "session_id": session_id,
        "payload": {"stated_objective": objective},
    }


def _scope_asserted(
    session_id: str = "sess-1",
    tool_id: str = "fs.read",
    advisory_flags: List[str] | None = None,
    policy_violations: List[str] | None = None,
) -> Dict:
    return {
        "event_type": "SCOPE_ASSERTED",
        "session_id": session_id,
        "advisory_flags": list(advisory_flags or []),
        "policy_violations": list(policy_violations or []),
        "payload": {"tool_id": tool_id},
    }


def _context_snapshot(
    session_id: str = "sess-1",
    turn_id: str = "turn-1",
    prompt: int = 100,
    completion: int = 50,
) -> Dict:
    return {
        "event_type": "CONTEXT_SNAPSHOT",
        "session_id": session_id,
        "payload": {
            "llm_turn_id": turn_id,
            "llm_prompt_tokens": prompt,
            "llm_completion_tokens": completion,
            "llm_cached_read_tokens": 0,
            "llm_cached_write_tokens": 0,
        },
    }


def _ok_session_with_undeclared() -> List[Dict]:
    return [
        _intent_declared(objective="export Q3 revenue"),
        _scope_asserted(tool_id="crm.list_invoices", policy_violations=["POL-001"]),
        _context_snapshot(turn_id="t1", prompt=900, completion=240),
        _context_snapshot(turn_id="t2", prompt=400, completion=100),
    ]


# ---------------------------------------------------------------------------
# Renderer tests
# ---------------------------------------------------------------------------


def test_render_cli_ok_status_includes_breakdown():
    result = compute_undeclared_intent_spend(_ok_session_with_undeclared())
    assert result["status"] == "ok"
    out = render_cli(result)
    assert "Undeclared-Intent Spend" in out
    assert "Total compute" in out
    assert "Undeclared" in out
    assert "Declared" in out
    assert "Undeclared turns" in out


def test_render_cli_no_token_data_status():
    result = {
        "session_id": "abc12345-xxx",
        "status": "no_token_data",
        "session_has_declared_intent": False,
        "total_tokens": 0,
        "undeclared_tokens": 0,
        "declared_tokens": 0,
        "undeclared_ratio": 0.0,
        "undeclared_percent": 0.0,
        "undeclared_turn_count": 0,
        "total_turn_count": 0,
        "undeclared_turns": [],
        "warnings": [],
        "unpaired_event_count": 0,
        "untokened_pair_count": 0,
        "dedupe_conflict_count": 0,
        "malformed_event_count": 0,
    }
    out = render_cli(result)
    assert "no_token_data" in out
    # No-token-data path must not print the agent-bound footer copy.
    assert "policy can intervene" not in out


def test_render_cli_agent_bound_footer_when_intent_declared():
    result = compute_undeclared_intent_spend(_ok_session_with_undeclared())
    out = render_cli(result)
    assert "policy can intervene" in out  # agent-bound branch
    assert "surface-level\nlimitation" not in out


def test_render_cli_surface_bound_footer_when_no_intent():
    # Session with undeclared turn but no INTENT_DECLARED.
    events = [
        _scope_asserted(advisory_flags=["INTENT_MISSING"]),
        _context_snapshot(turn_id="t1", prompt=100, completion=50),
    ]
    result = compute_undeclared_intent_spend(events)
    out = render_cli(result)
    assert "surface-level" in out
    assert "policy can intervene" not in out


def test_render_markdown_includes_canonical_footer():
    result = compute_undeclared_intent_spend(_ok_session_with_undeclared())
    out = render_markdown_report(result)
    assert "operators@crescerelabs.com" in out
    assert "getsentience.ai/launch-list" in out
    assert "consolidated view across all your runs" in out


def test_render_markdown_has_headline_table():
    result = compute_undeclared_intent_spend(_ok_session_with_undeclared())
    out = render_markdown_report(result)
    assert "## Headline" in out
    assert "| Metric | Tokens | Share |" in out
    assert "## Undeclared turns" in out


def test_render_markdown_no_token_data_skips_breakdown():
    result = {
        "session_id": "abc12345",
        "status": "no_token_data",
        "session_has_declared_intent": False,
        "total_tokens": 0,
        "undeclared_tokens": 0,
        "declared_tokens": 0,
        "undeclared_ratio": 0.0,
        "undeclared_percent": 0.0,
        "undeclared_turn_count": 0,
        "total_turn_count": 0,
        "undeclared_turns": [],
        "warnings": [],
        "unpaired_event_count": 0,
        "untokened_pair_count": 0,
        "dedupe_conflict_count": 0,
        "malformed_event_count": 0,
    }
    out = render_markdown_report(result)
    assert "## Headline" not in out
    # Footer must still render so the relationship surface ships.
    assert "operators@crescerelabs.com" in out


# ---------------------------------------------------------------------------
# CLI handler tests
# ---------------------------------------------------------------------------


def _write_trace(path: Path, events: List[Dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _make_args(**overrides) -> argparse.Namespace:
    base = {
        "target": None,
        "latest": False,
        "json": False,
        "save": False,
        "no_prompt": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_cli_json_mode_emits_full_dict(tmp_path, capsys):
    trace = tmp_path / "abcdef12.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    args = _make_args(target=str(trace), json=True)
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["status"] == "ok"
    assert parsed["session_id"] == "sess-1"
    assert parsed["undeclared_turn_count"] == 1
    assert parsed["total_turn_count"] == 2


def test_cli_no_prompt_suppresses_input(tmp_path, capsys, monkeypatch):
    trace = tmp_path / "abcdef12.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    # If input() got called the test would hang or raise; assert it's
    # never reached.
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **kw: pytest.fail("input() should not be called with --no-prompt"),
    )
    args = _make_args(target=str(trace), no_prompt=True)
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0


def test_cli_save_flag_writes_report(tmp_path, capsys, monkeypatch):
    trace = tmp_path / "abcdef12.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
    args = _make_args(target=str(trace), save=True)
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Saved to" in out
    saved = list(reports_dir.glob("undeclared-intent-*.md"))
    assert len(saved) == 1
    body = saved[0].read_text()
    assert "operators@crescerelabs.com" in body


def test_cli_save_suppressed_when_status_not_ok(tmp_path, capsys, monkeypatch):
    # Session with no token-bearing CONTEXT_SNAPSHOTs → no_token_data.
    trace = tmp_path / "abcdef12.jsonl"
    _write_trace(trace, [_intent_declared()])
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
    args = _make_args(target=str(trace), save=True)
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    err = capsys.readouterr().err
    assert "Skipping save" in err
    assert not reports_dir.exists() or not list(reports_dir.glob("*.md"))


def test_cli_prompt_yes_writes_report(tmp_path, capsys, monkeypatch):
    trace = tmp_path / "abcdef12.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")
    args = _make_args(target=str(trace))
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    saved = list(reports_dir.glob("undeclared-intent-*.md"))
    assert len(saved) == 1


def test_cli_prompt_default_enter_writes_report(tmp_path, capsys, monkeypatch):
    trace = tmp_path / "abcdef12.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
    args = _make_args(target=str(trace))
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    assert len(list(reports_dir.glob("*.md"))) == 1


def test_cli_prompt_no_does_not_write(tmp_path, capsys, monkeypatch):
    trace = tmp_path / "abcdef12.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "n")
    args = _make_args(target=str(trace))
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    assert not reports_dir.exists() or not list(reports_dir.glob("*.md"))


def test_cli_prompt_eof_does_not_save(tmp_path, capsys, monkeypatch):
    trace = tmp_path / "abcdef12.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)

    def _raise_eof(*a, **kw):
        raise EOFError()

    monkeypatch.setattr("builtins.input", _raise_eof)
    args = _make_args(target=str(trace))
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    assert not reports_dir.exists() or not list(reports_dir.glob("*.md"))


def test_cli_target_file_not_found(tmp_path, capsys):
    args = _make_args(target=str(tmp_path / "missing.jsonl"))
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err.lower()


def test_cli_session_prefix_match(tmp_path, capsys, monkeypatch):
    sink = tmp_path / "traces"
    sink.mkdir()
    trace = sink / "feedface-aaaa.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    monkeypatch.setattr(cli_ux, "_resolve_trace_dir", lambda: sink)

    args = _make_args(target="feedface", json=True)
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "ok"


def test_cli_session_prefix_ambiguous(tmp_path, capsys, monkeypatch):
    sink = tmp_path / "traces"
    sink.mkdir()
    _write_trace(sink / "aa-1.jsonl", _ok_session_with_undeclared())
    _write_trace(sink / "aa-2.jsonl", _ok_session_with_undeclared())
    monkeypatch.setattr(cli_ux, "_resolve_trace_dir", lambda: sink)

    args = _make_args(target="aa")
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "Ambiguous" in err


def test_cli_latest_flag(tmp_path, capsys, monkeypatch):
    sink = tmp_path / "traces"
    sink.mkdir()
    trace = sink / "only-one.jsonl"
    _write_trace(trace, _ok_session_with_undeclared())
    monkeypatch.setattr(cli_ux, "_resolve_trace_dir", lambda: sink)
    args = _make_args(latest=True, json=True)
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["status"] == "ok"


def test_cli_no_traces_available(tmp_path, capsys, monkeypatch):
    sink = tmp_path / "empty"
    sink.mkdir()
    monkeypatch.setattr(cli_ux, "_resolve_trace_dir", lambda: sink)
    args = _make_args()  # no target, no --latest
    rc = cli_ux.run_analyze_undeclared_intent(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "No sessions found" in err
