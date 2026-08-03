"""Tests for v0.2.5 CP6 — closed-loop demo byte-stability.

Three guarantees verified:

* The demo runs end-to-end without raising.
* ``session.jsonl`` regenerates byte-identically (the synthesized
  trace uses fixed event_ids + a fixed timestamp; serialization is
  deterministic).
* ``analyzer_output.md`` regenerates byte-identically (the analyzer
  + Markdown renderer are pure functions, and the input trace is
  byte-stable, so the output is too).

These guards catch unintended drift in the demo or in any of the
modules it composes (profile loader → analyzer → renderer). If any
of these tests fail with the demo script + analyzer + renderer
unchanged, something downstream broke byte-stability.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DEMO_PATH = REPO_ROOT / "examples" / "v025_closed_loop_demo.py"
SHOWCASE_DIR = REPO_ROOT / "examples" / "showcase" / "v025-closed-loop"
TRACE_PATH = SHOWCASE_DIR / "session.jsonl"
REPORT_PATH = SHOWCASE_DIR / "analyzer_output.md"
PROFILE_PATH = SHOWCASE_DIR / "profile.yaml"


def _run_demo_in_process() -> None:
    """Import + run the demo module in-process.

    Avoids spawning a subprocess (faster, plays nicely with coverage
    tooling). The demo's ``main()`` writes session.jsonl +
    analyzer_output.md as a side effect.
    """
    spec = importlib.util.spec_from_file_location(
        "_v025_demo_under_test", DEMO_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()


def test_demo_runs_without_error(capsys):
    """End-to-end smoke: the demo runs to completion + prints output."""
    _run_demo_in_process()
    out = capsys.readouterr().out
    assert "Closed-Loop Governance Demo" in out
    assert "High-consequence operations" in out
    assert "Task boundaries crossed" in out


def test_session_jsonl_is_byte_stable():
    """Regenerated session.jsonl must equal the pinned file bytes-for-bytes."""
    assert TRACE_PATH.is_file(), (
        "Pinned session.jsonl missing — run the demo to regenerate it"
    )
    pinned_bytes = TRACE_PATH.read_bytes()
    _run_demo_in_process()
    fresh_bytes = TRACE_PATH.read_bytes()
    assert pinned_bytes == fresh_bytes, (
        "session.jsonl drifted from the pinned bytes. If the change "
        "is intentional, re-run examples/v025_closed_loop_demo.py "
        "and commit the new fixture."
    )


def test_analyzer_output_md_is_byte_stable():
    """Regenerated analyzer_output.md must equal the pinned file bytes-for-bytes."""
    assert REPORT_PATH.is_file(), (
        "Pinned analyzer_output.md missing — run the demo to regenerate it"
    )
    pinned_bytes = REPORT_PATH.read_bytes()
    _run_demo_in_process()
    fresh_bytes = REPORT_PATH.read_bytes()
    assert pinned_bytes == fresh_bytes, (
        "analyzer_output.md drifted from the pinned bytes. If the "
        "change is intentional, re-run examples/v025_closed_loop_demo.py "
        "and commit the new fixture."
    )
