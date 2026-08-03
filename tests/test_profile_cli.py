"""Tests for v0.2.5 CP5 — `sentience profile` CLI subcommand group.

Six verbs covered: view, validate, export, import, edit, init.

Strategy: drive each handler directly with an argparse.Namespace
(rather than invoking the CLI via subprocess). This keeps tests fast
and avoids spawning child Python processes. We monkeypatch
``DEFAULT_PROFILE_PATH`` on both the loader and ux modules to a
tmp_path so tests never touch the operator's real
~/.sentience/profile.yaml.

CRITICAL guarantee verified by ``test_validate_does_not_mutate_file``:
read-only validation never modifies the source file (regression guard
for plan acceptance criterion).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import pytest

from sentience_governor.cli import ux as ux_mod
from sentience_governor.profile import loader as loader_mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_default_path(monkeypatch, tmp_path):
    """Point DEFAULT_PROFILE_PATH at a tmp file in both modules."""
    fake = tmp_path / "profile.yaml"
    monkeypatch.setattr(loader_mod, "DEFAULT_PROFILE_PATH", fake)
    monkeypatch.setattr(ux_mod, "DEFAULT_PROFILE_PATH", fake)
    return fake


def _ns(**kwargs) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for a handler call."""
    return argparse.Namespace(**kwargs)


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------


def test_view_prints_defaults_when_no_file(patched_default_path, capsys):
    rc = ux_mod.run_profile_view(_ns(resolved=False))
    assert rc == 0
    captured = capsys.readouterr()
    # Defaults banner goes to stderr.
    assert "No profile file found" in captured.err
    # YAML body goes to stdout. Must contain the canonical structure.
    assert "session_intent:" in captured.out
    assert "demand_at:" in captured.out


def test_view_prints_loaded_file_when_present(patched_default_path, capsys):
    patched_default_path.write_text(
        "schema_version: 1\n"
        "session_intent:\n"
        "  demand_at: first_write\n"
    )
    rc = ux_mod.run_profile_view(_ns(resolved=False))
    assert rc == 0
    captured = capsys.readouterr()
    # File-source banner + fingerprint go to stderr.
    assert "Source:" in captured.err
    assert "Fingerprint:" in captured.err
    # Body to stdout includes the operator's value.
    assert "first_write" in captured.out


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_passes_on_valid_profile(patched_default_path, capsys):
    patched_default_path.write_text(
        "schema_version: 1\n"
        "session_intent:\n"
        "  demand_at: session_start\n"
    )
    rc = ux_mod.run_profile_validate(_ns(path=None, strict=False))
    assert rc == 0


def test_validate_strict_rejects_unknown_keys(patched_default_path):
    patched_default_path.write_text(
        "schema_version: 1\n"
        "unknown_top_level: 42\n"
    )
    rc_lenient = ux_mod.run_profile_validate(_ns(path=None, strict=False))
    rc_strict = ux_mod.run_profile_validate(_ns(path=None, strict=True))
    assert rc_lenient == 0  # warns but passes
    assert rc_strict == 1  # strict errors


def test_validate_does_not_mutate_file(patched_default_path):
    """Critical regression guard: validate is READ-ONLY.

    The file's bytes + mtime must be identical before and after the
    validate handler runs. Plan §validation acceptance criterion.
    """
    contents = (
        "schema_version: 1\n"
        "session_intent:\n"
        "  demand_at: first_write\n"
        "  # comment that must survive\n"
    )
    patched_default_path.write_text(contents)
    pre_bytes = patched_default_path.read_bytes()
    pre_mtime = patched_default_path.stat().st_mtime_ns

    ux_mod.run_profile_validate(_ns(path=None, strict=False))

    post_bytes = patched_default_path.read_bytes()
    post_mtime = patched_default_path.stat().st_mtime_ns
    assert pre_bytes == post_bytes
    assert pre_mtime == post_mtime


def test_validate_edited_profile_reports_informational_not_mismatch(
    patched_default_path, capsys
):
    """F-V9: after the operator edits a generated profile, validate must
    report a non-alarming informational note, never the word MISMATCH."""
    from sentience_governor.profile import GovernanceProfile

    # Generate a real profile (writes a header hash), then edit the body
    # so the header hash goes stale.
    GovernanceProfile.defaults().export(patched_default_path)
    text = patched_default_path.read_text()
    patched_default_path.write_text(text + "\nhigh_consequence:\n  tools:\n    - db.delete\n")

    rc = ux_mod.run_profile_validate(_ns(path=None, strict=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "MISMATCH" not in out
    assert "stale" in out.lower()
    assert "recomputed hash" in out.lower()


def test_validate_unedited_profile_reports_ok(patched_default_path, capsys):
    """A freshly generated (unedited) profile still reports content_hash OK."""
    from sentience_governor.profile import GovernanceProfile

    GovernanceProfile.defaults().export(patched_default_path)
    rc = ux_mod.run_profile_validate(_ns(path=None, strict=False))
    out = capsys.readouterr().out
    assert rc == 0
    assert "content_hash: OK" in out


def test_validate_no_file_returns_zero(patched_default_path, capsys):
    """When no profile file exists, defaults are valid by construction
    and the handler exits 0 (validation is not a precondition for
    using the runtime — defaults always work)."""
    rc = ux_mod.run_profile_validate(_ns(path=None, strict=False))
    assert rc == 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_export_writes_header_and_body(patched_default_path, tmp_path, capsys):
    dest = tmp_path / "exported.yaml"
    rc = ux_mod.run_profile_export(_ns(path=str(dest)))
    assert rc == 0
    text = dest.read_text()
    # Header: schema version, content hash, generated timestamp.
    assert "# Schema version:" in text
    assert "# Content hash: sha256:" in text
    assert "# Generated:" in text
    # Body: canonical sections present.
    assert "session_intent:" in text


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_import_validates_and_installs(patched_default_path, tmp_path):
    src = tmp_path / "src.yaml"
    src.write_text(
        "schema_version: 1\n"
        "session_intent:\n"
        "  demand_at: first_write\n"
    )
    rc = ux_mod.run_profile_import(_ns(path=str(src)))
    assert rc == 0
    # Default path now exists with the imported content (header added).
    assert patched_default_path.is_file()
    body = patched_default_path.read_text()
    assert "first_write" in body
    assert "# Content hash:" in body


def test_import_refuses_missing_source(patched_default_path, tmp_path):
    rc = ux_mod.run_profile_import(_ns(path=str(tmp_path / "nope.yaml")))
    assert rc == 1
    # Default path must NOT have been created.
    assert not patched_default_path.exists()


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_starter_profile(patched_default_path, capsys):
    rc = ux_mod.run_profile_init(_ns())
    assert rc == 0
    assert patched_default_path.is_file()
    text = patched_default_path.read_text()
    assert "# Schema version:" in text
    assert "session_intent:" in text


def test_init_refuses_to_overwrite_existing_file(patched_default_path):
    patched_default_path.write_text("pre-existing content\n")
    rc = ux_mod.run_profile_init(_ns())
    assert rc == 1
    # File contents unchanged — no silent overwrite.
    assert patched_default_path.read_text() == "pre-existing content\n"


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


def test_edit_errors_when_no_file(patched_default_path, capsys):
    rc = ux_mod.run_profile_edit(_ns())
    assert rc == 1
    err = capsys.readouterr().err
    assert "no profile" in err.lower()
    assert "init" in err.lower()  # hint to run init


def test_edit_invokes_editor(
    patched_default_path, monkeypatch, capsys
):
    """Verify edit launches $EDITOR via subprocess.run with the
    correct args. We replace subprocess.run with a stub that records
    the call so we don't actually spawn an editor."""
    patched_default_path.write_text("schema_version: 1\n")
    monkeypatch.setenv("EDITOR", "fake-editor")

    calls = []

    class _FakeResult:
        returncode = 0

    def _fake_run(args, *_, **__):
        calls.append(args)
        return _FakeResult()

    import subprocess
    monkeypatch.setattr(subprocess, "run", _fake_run)

    rc = ux_mod.run_profile_edit(_ns())
    assert rc == 0
    assert calls == [["fake-editor", str(patched_default_path)]]


def test_edit_falls_back_when_editor_unset(
    patched_default_path, monkeypatch
):
    """F-V8: with $EDITOR/$VISUAL unset, fall back to nano/vim/vi rather
    than hard-erroring (macOS has no $EDITOR by default)."""
    patched_default_path.write_text("schema_version: 1\n")
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)

    import shutil
    # Only 'nano' is on PATH.
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/nano" if name == "nano" else None)

    calls = []

    class _FakeResult:
        returncode = 0

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, *_, **__: calls.append(args) or _FakeResult())

    rc = ux_mod.run_profile_edit(_ns())
    assert rc == 0
    assert calls == [["nano", str(patched_default_path)]]


def test_edit_prefers_visual_over_editor(patched_default_path, monkeypatch):
    """$VISUAL takes precedence over $EDITOR."""
    patched_default_path.write_text("schema_version: 1\n")
    monkeypatch.setenv("VISUAL", "vis-ed")
    monkeypatch.setenv("EDITOR", "ed-ed")

    calls = []

    class _FakeResult:
        returncode = 0

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, *_, **__: calls.append(args) or _FakeResult())

    assert ux_mod.run_profile_edit(_ns()) == 0
    assert calls == [["vis-ed", str(patched_default_path)]]


def test_edit_macos_open_e_fallback(patched_default_path, monkeypatch):
    """On macOS with no editor env/terminal editor, fall back to open -e
    (multi-arg launcher), and the path is appended to the list prefix."""
    patched_default_path.write_text("schema_version: 1\n")
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")

    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/open" if name == "open" else None)

    calls = []

    class _FakeResult:
        returncode = 0

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda args, *_, **__: calls.append(args) or _FakeResult())

    assert ux_mod.run_profile_edit(_ns()) == 0
    assert calls == [["open", "-e", str(patched_default_path)]]


def test_edit_errors_when_no_editor_available(
    patched_default_path, monkeypatch, capsys
):
    """Only when NOTHING is resolvable does edit error."""
    patched_default_path.write_text("schema_version: 1\n")
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)

    rc = ux_mod.run_profile_edit(_ns())
    assert rc == 1
    assert "no editor found" in capsys.readouterr().err.lower()
