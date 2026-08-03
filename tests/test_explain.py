"""IR-5 (v0.2.9): `sentience explain` — machine-readable methodology.

Methodology-only in v0.2.9: no `explain <CODE>` mode. Covers the
human-readable surface, the --json surface (valid + deterministic), the
per-turn (not per-tool) attribution boundary, and the no-positional
(methodology-only) contract.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from sentience_governor.analyze.methodology import (
    METHODOLOGY_VERSION,
    build_methodology,
)
from sentience_governor.cli.ux import run_explain


def _run(json_mode: bool, capsys) -> str:
    rc = run_explain(argparse.Namespace(json=json_mode))
    assert rc == 0
    return capsys.readouterr().out


class TestExplainHumanReadable:
    def test_lists_the_four_token_classes(self, capsys):
        out = _run(False, capsys)
        assert "Token classes" in out
        for label in ("prompt", "completion", "cached read", "cached write"):
            assert label in out

    def test_states_dedupe_by_llm_turn_id(self, capsys):
        out = _run(False, capsys)
        assert "deduped by llm_turn_id" in out
        # requestId is named as the model-invocation boundary, not the
        # canonical field.
        assert "requestId" in out

    def test_states_per_turn_not_per_tool_boundary(self, capsys):
        """The load-bearing IR-3 truth: attribution stops at the turn."""
        out = _run(False, capsys)
        assert "metered per model turn, not per tool call" in out
        assert "never to an individual tool" in out

    def test_lists_operation_type_enum(self, capsys):
        out = _run(False, capsys)
        for op in ("READ", "WRITE", "DELETE", "EXECUTE"):
            assert op in out

    def test_states_join_key_semantics(self, capsys):
        out = _run(False, capsys)
        assert "tool_use_id" in out and "llm_turn_id" in out

    def test_wording_guard_no_affirmative_per_tool_spend(self, capsys):
        """The methodology must frame tokens as turn-attributed. The only
        'spent' mention is the explicit negation that per-tool spend is
        NOT measurable."""
        out = _run(False, capsys)
        assert "'tokens tool X spent' is not" in out
        # No affirmative "tool spent N tokens" phrasing.
        assert "tool spent" not in out.lower()


class TestExplainJson:
    def test_json_is_valid_and_has_stable_keys(self, capsys):
        out = _run(True, capsys)
        d = json.loads(out)
        assert d["methodology_version"] == METHODOLOGY_VERSION
        assert set(d.keys()) == set(build_methodology().keys())
        assert d["operation_types"] == ["READ", "WRITE", "DELETE", "EXECUTE"]
        assert set(d["token_classes"].keys()) == {
            "prompt", "completion", "cached_read", "cached_write",
        }

    def test_json_is_deterministic(self, capsys):
        first = _run(True, capsys)
        second = _run(True, capsys)
        assert first == second

    def test_json_matches_builder(self, capsys):
        out = _run(True, capsys)
        assert json.loads(out) == build_methodology()


class TestExplainMethodologyOnly:
    def test_rejects_a_code_positional(self):
        """v0.2.9 is methodology-only — `explain <CODE>` is not a mode.
        argparse must reject an unexpected positional."""
        result = subprocess.run(
            [sys.executable, "-m", "sentience_governor.cli.ux",
             "explain", "POL-001"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0
        assert "unrecognized arguments: POL-001" in result.stderr

    def test_bare_explain_succeeds(self):
        result = subprocess.run(
            [sys.executable, "-m", "sentience_governor.cli.ux", "explain"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0
        assert "Sentience methodology" in result.stdout
