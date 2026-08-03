"""Tests for v0.2.6 CP7 — pulse showcase byte-stability.

Three guarantees verified, per plan v3.6 §CP7 sanity gate:

* The demo runs end-to-end without raising.
* Every `session.jsonl` regenerates byte-identically (synthesized
  traces pin event_ids + timestamps; serialization is deterministic).
* Every `pulse_output.md` regenerates byte-identically (the
  analyzer + pulse renderer are pure functions; pinned input ⇒
  pinned output).

The v0.2.5 closed-loop showcase's pulse cross-link is part of the
same byte-stability surface — the demo regenerates it from the
existing v0.2.5 trace and the test pins it alongside the v0.2.6
cases.

If any of these tests fail with the demo script + analyzer +
renderer unchanged, something downstream broke byte-stability.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_PATH = REPO_ROOT / "examples" / "v026_pulse_demo.py"
SHOWCASE_DIR = REPO_ROOT / "examples" / "showcase" / "v026-pulse"
V025_SHOWCASE_DIR = REPO_ROOT / "examples" / "showcase" / "v025-closed-loop"

CASES = ("clean", "missing_intent", "mixed_violations")


def _run_demo_in_process() -> None:
    """Import + run the demo module in-process.

    Avoids spawning a subprocess (faster, plays nicely with coverage
    tooling). The demo's main() writes the per-case session.jsonl +
    pulse_output.md + v0.2.5 retrofit as side effects.
    """
    spec = importlib.util.spec_from_file_location(
        "_v026_pulse_demo_under_test", DEMO_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()


# ---------------------------------------------------------------------------
# End-to-end smoke
# ---------------------------------------------------------------------------


class TestDemoRunsCleanly:
    def test_demo_runs_without_error(self, capsys):
        _run_demo_in_process()
        out = capsys.readouterr().out
        # The header line and per-case summaries are part of the
        # operator-facing surface — confirm they appear.
        assert "Sentience Pulse Demo" in out
        for case in CASES:
            assert f"[{case}]" in out
        assert "[v025-retrofit]" in out


# ---------------------------------------------------------------------------
# session.jsonl byte-stability — one per case
# ---------------------------------------------------------------------------


class TestSessionJsonlByteStability:
    @pytest.mark.parametrize("case", CASES)
    def test_session_jsonl_byte_stable(self, case):
        trace_path = SHOWCASE_DIR / case / "session.jsonl"
        assert trace_path.is_file(), (
            f"Pinned session.jsonl missing for {case} — "
            f"run the demo to regenerate it"
        )
        pinned = trace_path.read_bytes()
        _run_demo_in_process()
        fresh = trace_path.read_bytes()
        assert pinned == fresh, (
            f"{case}: session.jsonl drifted from pinned bytes. If "
            "the change is intentional, re-run "
            "`examples/v026_pulse_demo.py` and commit the new fixture."
        )


# ---------------------------------------------------------------------------
# pulse_output.md byte-stability — one per case + v0.2.5 retrofit
# ---------------------------------------------------------------------------


class TestPulseOutputByteStability:
    @pytest.mark.parametrize("case", CASES)
    def test_v026_pulse_output_byte_stable(self, case):
        pulse_path = SHOWCASE_DIR / case / "pulse_output.md"
        assert pulse_path.is_file(), (
            f"Pinned pulse_output.md missing for {case} — "
            f"run the demo to regenerate it"
        )
        pinned = pulse_path.read_bytes()
        _run_demo_in_process()
        fresh = pulse_path.read_bytes()
        assert pinned == fresh, (
            f"{case}: pulse_output.md drifted from pinned bytes. If "
            "the change is intentional, re-run "
            "`examples/v026_pulse_demo.py` and commit the new fixture."
        )

    def test_v025_retrofit_pulse_output_byte_stable(self):
        """The v0.2.5 cross-link pulse_output.md must also pin
        byte-stably — it shares the same generator path."""
        v025_pulse = V025_SHOWCASE_DIR / "pulse_output.md"
        assert v025_pulse.is_file(), (
            "Pinned v0.2.5 retrofit pulse_output.md missing — "
            "run the demo to regenerate it"
        )
        pinned = v025_pulse.read_bytes()
        _run_demo_in_process()
        fresh = v025_pulse.read_bytes()
        assert pinned == fresh


# ---------------------------------------------------------------------------
# Sanity gates — content-level checks across the showcase
# ---------------------------------------------------------------------------


class TestShowcaseContentSanity:
    """Sanity gates beyond byte-stability — ensures the showcase
    actually tells the three operator stories the README promises.
    """

    def test_clean_case_has_interpretation_block(self):
        body = (
            SHOWCASE_DIR / "clean" / "pulse_output.md"
        ).read_text()
        assert "## Interpretation" in body
        assert "No policy violations recorded." in body
        # v3.1 qualified clean-session wording.
        flat = " ".join(body.split())
        assert (
            "no policy violations were recorded against the rules active"
            in flat
        )

    def test_missing_intent_case_surfaces_surface_bound_framing(self):
        body = (
            SHOWCASE_DIR / "missing_intent" / "pulse_output.md"
        ).read_text()
        # Surface-bound framing must appear so operators read the
        # 100%-undeclared result correctly.
        assert "surface-bound" in body or "Claude Code today" in body
        # POL-001 must surface in the burn-rate section.
        assert "POL-001" in body

    def test_mixed_case_has_multiple_distinct_rules(self):
        body = (
            SHOWCASE_DIR / "mixed_violations" / "pulse_output.md"
        ).read_text()
        # All three planned POL firings present.
        for rule in ("POL-001", "POL-003", "POL-005"):
            assert rule in body, f"{rule} missing from mixed showcase"
        # Non-additivity note is present (full `notes` text in
        # Markdown form per CP2 spec).
        assert "not additive" in body

    def test_v025_retrofit_is_clean_session(self):
        body = (
            V025_SHOWCASE_DIR / "pulse_output.md"
        ).read_text()
        assert "No policy violations recorded." in body
        assert "## Interpretation" in body
