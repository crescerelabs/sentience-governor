"""CLI tests for `sentience analyze policy-violations` (v0.2.6 CP3).

Mirrors the v0.2.4 sibling at tests/test_analyze_cli.py (CLI tests for
`sentience analyze undeclared-intent`). v0.2.6 is adoption-surface
heavy; this test file enforces the 11 explicit coverage items called
for in plan v3.5 fix #8:

  1. `sentience analyze policy-violations --help` renders cleanly
  2. positional target path resolves
  3. `--latest` resolves to the newest session
  4. `--json` mode emits valid JSON and includes the burn-rate result
  5. `--save` flow writes the Markdown report
  6. `--no-prompt` flow suppresses interactive prompts
  7. interactive save prompt fires on `ok` / `no_violations` status
  8. non-eligible-status skip-save behavior (standalone analyzer
     keeps the v0.2.4 skip-save-on-non-ok contract — distinct from
     pulse). The save-eligibility expansion is: ok AND no_violations
     are both eligible (per CP3 spec); partial / no_token_data /
     no_turns are NOT.
  9. malformed / missing trace handling
 10. output includes the burn-rate definition phrase: "Compute
     associated with turns where policy rules fired"
 11. output uses association language, NOT savings/causality language
     (regression guard — banned words: reclaim, save, prevent,
     would have)
 12. Markdown report path:
     ~/.sentience/reports/policy-violations-<sid-prefix>-<timestamp>.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sentience_governor.cli import ux as cli_ux


# ---------------------------------------------------------------------------
# Synthetic event constructors — mirror the v0.2.4 test pattern.
# ---------------------------------------------------------------------------


def _agent_registered(
    session_id: str = "sess-pv-1",
    *,
    policy_violations: List[str] | None = None,
) -> Dict:
    return {
        "event_id": "evt-reg",
        "event_type": "AGENT_REGISTERED",
        "session_id": session_id,
        "event_sequence_number": 1,
        "agent_id": "test-agent",
        "deployment_mode": "vendor_managed",
        "timestamp_utc": "2026-05-28T00:00:00.000Z",
        "primitive": "REGISTRATION",
        "payload": {
            "agent_id": "test-agent",
            "agent_version": "1.0.0",
            "vendor_id": "test-vendor",
            "deployment_mode": "vendor_managed",
            "declared_capabilities": ["fs.write"],
            "owner_claim": "operator@example.com",
            "policy_context": "test",
            "profile_loaded": True,
            "profile_schema_version": 1,
        },
        "advisory_flags": [],
        "policy_violations": list(policy_violations or []),
        "simulated_consequence": None,
        "pass_through": True,
        "profile_fingerprint": "synth0pv0001",
    }


def _scope_asserted(
    session_id: str = "sess-pv-1",
    seq: int = 2,
    *,
    tool_id: str = "fs.write",
    op: str = "WRITE",
    policy_violations: List[str] | None = None,
) -> Dict:
    return {
        "event_id": f"evt-scope-{seq}",
        "event_type": "SCOPE_ASSERTED",
        "session_id": session_id,
        "event_sequence_number": seq,
        "agent_id": "test-agent",
        "deployment_mode": "vendor_managed",
        "timestamp_utc": "2026-05-28T00:00:00.000Z",
        "primitive": "SCOPE",
        "payload": {
            "tool_id": tool_id,
            "asserted_permissions": [op.lower()],
            "target_system": "test-target",
            "operation_type": op,
        },
        "advisory_flags": [],
        "policy_violations": list(policy_violations or []),
        "simulated_consequence": None,
        "pass_through": True,
        "profile_fingerprint": "synth0pv0001",
    }


def _context_snapshot(
    session_id: str = "sess-pv-1",
    seq: int = 3,
    *,
    turn_id: str = "turn-1",
    tokens: int | None = 1000,
    policy_violations: List[str] | None = None,
) -> Dict:
    payload: Dict[str, Any] = {
        "data_classifications": ["internal"],
        "classification_source": "explicit",
        "provenance": [],
        "retention_flags": [],
        "context_size_tokens": tokens or 0,
    }
    if tokens is not None:
        payload["llm_prompt_tokens"] = tokens
        payload["llm_completion_tokens"] = tokens // 5
    if turn_id is not None:
        payload["llm_turn_id"] = turn_id
    return {
        "event_id": f"evt-ctx-{seq}",
        "event_type": "CONTEXT_SNAPSHOT",
        "session_id": session_id,
        "event_sequence_number": seq,
        "agent_id": "test-agent",
        "deployment_mode": "vendor_managed",
        "timestamp_utc": "2026-05-28T00:00:00.000Z",
        "primitive": "CONTEXT",
        "payload": payload,
        "advisory_flags": [],
        "policy_violations": list(policy_violations or []),
        "simulated_consequence": None,
        "pass_through": True,
        "profile_fingerprint": "synth0pv0001",
    }


def _ok_session_with_pol_001() -> List[Dict]:
    """Session that produces status=ok with POL-001 firing on a turn
    that has populated tokens."""
    return [
        _agent_registered(),
        _scope_asserted(seq=2, policy_violations=["POL-001"]),
        _context_snapshot(seq=3, turn_id="turn-1", tokens=1000),
        _scope_asserted(seq=4, tool_id="fs.read", op="READ"),
        _context_snapshot(seq=5, turn_id="turn-2", tokens=800),
    ]


def _clean_session() -> List[Dict]:
    """Session that produces status=no_violations — turns + tokens
    present, no policy_violations anywhere."""
    return [
        _agent_registered(),
        _scope_asserted(seq=2),
        _context_snapshot(seq=3, turn_id="turn-1", tokens=1000),
        _context_snapshot(seq=4, turn_id="turn-2", tokens=800),
    ]


def _no_token_session() -> List[Dict]:
    """Session that produces status=no_token_data — turns exist but
    no populated tokens."""
    return [
        _agent_registered(),
        _scope_asserted(seq=2, policy_violations=["POL-001"]),
        _context_snapshot(seq=3, turn_id="turn-1", tokens=None),
    ]


def _no_turns_session() -> List[Dict]:
    """Session that produces status=no_turns — no CONTEXT_SNAPSHOTs
    with llm_turn_id."""
    return [
        _agent_registered(),
        _scope_asserted(seq=2, policy_violations=["POL-001"]),
        _context_snapshot(seq=3, turn_id=None, tokens=None),
    ]


def _write_trace(path: Path, events: List[Dict]) -> None:
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _make_args(**overrides) -> argparse.Namespace:
    base = {
        "target": None,
        "latest": False,
        "json": False,
        "save": False,
        "no_prompt": False,
        "showcase": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ===========================================================================
# Coverage item 1 — `--help` renders cleanly
# ===========================================================================


class TestHelpSurface:
    def test_policy_violations_help_renders(self):
        """`sentience analyze policy-violations --help` exits 0 and
        prints help text including all expected flags."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sentience_governor.cli.ux",
                "analyze",
                "policy-violations",
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "policy-violations" in result.stdout
        # All expected flags surface in --help output.
        for flag in ("--latest", "--showcase", "--json", "--save", "--no-prompt"):
            assert flag in result.stdout

    def test_analyze_subcommand_lists_policy_violations(self):
        """`sentience analyze --help` lists policy-violations alongside
        undeclared-intent in the subparser registry."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sentience_governor.cli.ux",
                "analyze",
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "undeclared-intent" in result.stdout
        assert "policy-violations" in result.stdout


# ===========================================================================
# Coverage item 2 — positional target path resolves
# ===========================================================================


class TestPositionalTarget:
    def test_explicit_file_path(self, tmp_path, capsys):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        args = _make_args(target=str(trace), json=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["status"] == "ok"
        assert parsed["session_id"] == "sess-pv-1"


# ===========================================================================
# Coverage item 3 — `--latest` resolves to the newest session
# ===========================================================================


class TestLatestFlag:
    def test_latest_resolves_to_newest(self, tmp_path, capsys, monkeypatch):
        sink = tmp_path / "traces"
        sink.mkdir()
        trace = sink / "only-one.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        monkeypatch.setattr(cli_ux, "_resolve_trace_dir", lambda: sink)
        args = _make_args(latest=True, json=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["status"] == "ok"


# ===========================================================================
# Coverage item 4 — `--json` mode emits valid JSON + burn-rate dict
# ===========================================================================


class TestJsonMode:
    def test_json_emits_full_burn_rate_result_dict(self, tmp_path, capsys):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        args = _make_args(target=str(trace), json=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        # Burn-rate result shape per plan v3.6.
        assert parsed["analyzer"] == "policy_violation_burn_rate"
        assert parsed["analyzer_version"] == "0.2.6"
        assert parsed["status"] == "ok"
        assert "by_rule" in parsed
        assert "POL-001" in parsed["by_rule"]
        assert parsed["violation_firing_turns"] == 1
        assert parsed["by_rule"]["POL-001"]["turn_count"] == 1
        # Burn-rate-specific top-level fields.
        for key in (
            "schema_version",
            "violation_associated_tokens",
            "notes",
            "notes_short",
            "warnings",
            "unknown_rule_count",
        ):
            assert key in parsed

    def test_json_mode_suppresses_prompt_and_render(
        self, tmp_path, capsys, monkeypatch
    ):
        """--json should bypass both the human-readable render AND the
        interactive prompt."""
        monkeypatch.setattr(
            "builtins.input",
            lambda *a, **kw: pytest.fail("input() should not be called with --json"),
        )
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        args = _make_args(target=str(trace), json=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        out = capsys.readouterr().out
        # Output is JSON only; no human-readable banner.
        assert "Policy-Violation Burn Rate" not in out


# ===========================================================================
# Coverage item 5 — `--save` flow writes the Markdown report
# ===========================================================================


class TestSaveFlag:
    def test_save_writes_report(self, tmp_path, capsys, monkeypatch):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
        args = _make_args(target=str(trace), save=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "Saved to" in out
        saved = list(reports_dir.glob("policy-violations-*.md"))
        assert len(saved) == 1
        body = saved[0].read_text()
        # Canonical footer present.
        assert "operators@crescerelabs.com" in body
        # Burn-rate Markdown title present.
        assert "Policy-Violation Burn Rate" in body
        # By-rule table present.
        assert "POL-001" in body


# ===========================================================================
# Coverage item 6 — `--no-prompt` flow suppresses interactive prompts
# ===========================================================================


class TestNoPromptFlag:
    def test_no_prompt_suppresses_input(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(
            "builtins.input",
            lambda *a, **kw: pytest.fail(
                "input() should not be called with --no-prompt"
            ),
        )
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        args = _make_args(target=str(trace), no_prompt=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0


# ===========================================================================
# Coverage item 7 — interactive save prompt fires on ok / no_violations
# ===========================================================================


class TestInteractivePrompt:
    def test_prompt_fires_on_ok_status(self, tmp_path, capsys, monkeypatch):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")
        args = _make_args(target=str(trace))
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        saved = list(reports_dir.glob("policy-violations-*.md"))
        assert len(saved) == 1

    def test_prompt_fires_on_no_violations_status(
        self, tmp_path, capsys, monkeypatch
    ):
        """v3.6 CP3 expansion: no_violations is ALSO save-eligible
        for the standalone analyzer (clean-session report is
        explicitly shareable). Distinct from v0.2.4 undeclared-intent
        which only saves on `ok`."""
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _clean_session())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "y")
        args = _make_args(target=str(trace))
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        saved = list(reports_dir.glob("policy-violations-*.md"))
        assert len(saved) == 1

    def test_prompt_default_enter_writes_report(
        self, tmp_path, capsys, monkeypatch
    ):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "")
        args = _make_args(target=str(trace))
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        assert len(list(reports_dir.glob("*.md"))) == 1

    def test_prompt_no_does_not_write(self, tmp_path, capsys, monkeypatch):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
        monkeypatch.setattr("builtins.input", lambda *a, **kw: "n")
        args = _make_args(target=str(trace))
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        assert not reports_dir.exists() or not list(reports_dir.glob("*.md"))

    def test_prompt_eof_does_not_save(self, tmp_path, capsys, monkeypatch):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)

        def _raise_eof(*a, **kw):
            raise EOFError()

        monkeypatch.setattr("builtins.input", _raise_eof)
        args = _make_args(target=str(trace))
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        assert not reports_dir.exists() or not list(reports_dir.glob("*.md"))


# ===========================================================================
# Coverage item 8 — non-eligible-status skip-save behavior
# ===========================================================================


class TestNonEligibleSaveBehavior:
    """Standalone analyzer keeps the v0.2.4 skip-save-on-non-ok contract:
    no_token_data, no_turns, and partial are NOT save-eligible. Only
    ok and no_violations (CP3 expansion) trigger save. This is distinct
    from pulse's save-everything-always behavior (CP6).
    """

    def test_save_suppressed_when_status_no_token_data(
        self, tmp_path, capsys, monkeypatch
    ):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _no_token_session())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
        args = _make_args(target=str(trace), save=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        err = capsys.readouterr().err
        assert "Skipping save" in err
        assert "no_token_data" in err
        assert not reports_dir.exists() or not list(reports_dir.glob("*.md"))

    def test_save_suppressed_when_status_no_turns(
        self, tmp_path, capsys, monkeypatch
    ):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _no_turns_session())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
        args = _make_args(target=str(trace), save=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        err = capsys.readouterr().err
        assert "Skipping save" in err
        assert "no_turns" in err

    def test_prompt_suppressed_when_non_eligible_status(
        self, tmp_path, capsys, monkeypatch
    ):
        """When status is non-eligible, the interactive prompt should
        not fire at all (no input() call)."""
        monkeypatch.setattr(
            "builtins.input",
            lambda *a, **kw: pytest.fail(
                "input() should not be called for non-eligible status"
            ),
        )
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _no_token_session())
        args = _make_args(target=str(trace))
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0


# ===========================================================================
# Coverage item 9 — malformed / missing trace handling
# ===========================================================================


class TestTraceResolutionErrors:
    def test_file_not_found(self, tmp_path, capsys):
        args = _make_args(target=str(tmp_path / "missing.jsonl"))
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err.lower()

    def test_session_prefix_match(self, tmp_path, capsys, monkeypatch):
        sink = tmp_path / "traces"
        sink.mkdir()
        trace = sink / "feedface-aaaa.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        monkeypatch.setattr(cli_ux, "_resolve_trace_dir", lambda: sink)
        args = _make_args(target="feedface", json=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["status"] == "ok"

    def test_session_prefix_ambiguous(self, tmp_path, capsys, monkeypatch):
        sink = tmp_path / "traces"
        sink.mkdir()
        _write_trace(sink / "aa-1.jsonl", _ok_session_with_pol_001())
        _write_trace(sink / "aa-2.jsonl", _ok_session_with_pol_001())
        monkeypatch.setattr(cli_ux, "_resolve_trace_dir", lambda: sink)
        args = _make_args(target="aa")
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "Ambiguous" in err

    def test_empty_trace_dir(self, tmp_path, capsys, monkeypatch):
        sink = tmp_path / "empty"
        sink.mkdir()
        monkeypatch.setattr(cli_ux, "_resolve_trace_dir", lambda: sink)
        args = _make_args()
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "No sessions found" in err


# ===========================================================================
# Coverage item 10 — output includes burn-rate definition phrase
# ===========================================================================


class TestBurnRateDefinitionPhrase:
    def test_definition_phrase_in_ok_render(self, tmp_path, capsys):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        args = _make_args(target=str(trace), no_prompt=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        out = capsys.readouterr().out
        # Plan v3.6 §"CLI output" canonical phrase.
        assert (
            "Compute associated with turns where policy rules fired"
            in out
        )


# ===========================================================================
# Coverage item 11 — attribution discipline (association language only)
# ===========================================================================


class TestAttributionDiscipline:
    """Plan v3.6 + v2 wording discipline: burn-rate copy uses
    association language only, never savings/causality wording.
    Regression guard for the v3 attribution-discipline rewrite.
    """

    @pytest.mark.parametrize(
        "session_factory,description",
        [
            (_ok_session_with_pol_001, "ok status"),
            (_clean_session, "no_violations status"),
            (_no_token_session, "no_token_data status"),
            (_no_turns_session, "no_turns status"),
        ],
    )
    def test_no_savings_language_in_render(
        self, tmp_path, capsys, session_factory, description
    ):
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, session_factory())
        args = _make_args(target=str(trace), no_prompt=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        out = capsys.readouterr().out.lower()
        banned = ["reclaim", "would save", "could save", "would prevent",
                  "would have been"]
        for word in banned:
            assert word not in out, (
                f"banned attribution-discipline word {word!r} found in "
                f"{description} CLI render"
            )


# ===========================================================================
# Coverage item 12 — Markdown report path shape
# ===========================================================================


class TestMarkdownReportPath:
    def test_path_shape_matches_spec(self, tmp_path, capsys, monkeypatch):
        """Markdown report path:
        ~/.sentience/reports/policy-violations-<sid-prefix>-<timestamp>.md
        """
        trace = tmp_path / "abc12345.jsonl"
        _write_trace(trace, _ok_session_with_pol_001())
        reports_dir = tmp_path / "reports"
        monkeypatch.setattr(cli_ux, "_REPORTS_DIR", reports_dir)
        args = _make_args(target=str(trace), save=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        assert rc == 0
        saved = list(reports_dir.glob("*.md"))
        assert len(saved) == 1
        fname = saved[0].name
        # Filename shape: policy-violations-<sid-prefix>-<timestamp>.md
        assert fname.startswith("policy-violations-")
        assert fname.endswith(".md")
        # sid-prefix should be the first 12 chars of the session_id.
        assert "sess-pv-1" in fname
        # Timestamp pattern: YYYYMMDDTHHMMSS
        # (one capital T between date and time portions).
        parts = fname[len("policy-violations-"):-len(".md")].split("-")
        # parts[-1] is the timestamp (sid may itself contain dashes)
        timestamp = parts[-1]
        assert len(timestamp) == 15  # YYYYMMDDTHHMMSS
        assert timestamp[8] == "T"


# ===========================================================================
# --showcase flag (bonus coverage — same surface as undeclared-intent)
# ===========================================================================


class TestShowcaseFlag:
    def test_showcase_renders_bundled_trace(self, tmp_path, capsys):
        """--showcase analyzes the bundled closed-loop trace."""
        args = _make_args(showcase=True, no_prompt=True)
        rc = cli_ux.run_analyze_policy_violations(args)
        # Either rc==0 with valid render OR rc==1 with "not found" (if
        # showcase trace not bundled in this install). Both are valid.
        if rc == 0:
            out = capsys.readouterr().out
            assert "Policy-Violation Burn Rate" in out
        else:
            err = capsys.readouterr().err
            assert "showcase trace not found" in err.lower()
