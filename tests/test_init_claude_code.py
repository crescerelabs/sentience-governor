"""F-V1: `sentience init claude-code` — hook wiring + idempotent merge.

Covers the three cases the v0.2.5.2 plan calls out:
  (a) no existing .claude/settings.json
  (b) settings.json present with unrelated hooks
  (c) already wired — re-run is a no-op (no duplicate entries)
"""

import argparse
import json
from pathlib import Path

import pytest

from sentience_governor.cli import ux


FAKE_HOOK = "/fake/bin/sentience-claude-code-hook"


@pytest.fixture
def patched_binary(monkeypatch):
    """Force a deterministic hook-binary path regardless of test env."""
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: FAKE_HOOK)


def _run(path: Path) -> int:
    # Hook-focused tests opt out of skill install (skills get their own
    # CP3 suite); keeps these from touching the real ~/.claude/skills/.
    args = argparse.Namespace(
        path=str(path), no_skills=True, project=False, force=False
    )
    return ux.run_init_claude_code(args)


def _read_settings(project: Path) -> dict:
    return json.loads((project / ".claude" / "settings.json").read_text())


def test_init_creates_settings_when_absent(tmp_path, patched_binary, capsys):
    rc = _run(tmp_path)
    assert rc == 0

    settings = _read_settings(tmp_path)
    for event in ("PreToolUse", "PostToolUse", "SessionEnd"):
        entries = settings["hooks"][event]
        assert len(entries) == 1
        assert entries[0]["hooks"][0]["command"] == FAKE_HOOK
        assert entries[0]["matcher"] == ""

    out = capsys.readouterr().out
    assert "Hook installed" in out
    assert "sentience status" in out


def test_init_preserves_unrelated_settings_and_hooks(tmp_path, patched_binary):
    claude = tmp_path / ".claude"
    claude.mkdir()
    existing = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "someOtherSetting": True,
        "hooks": {
            "PreToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": "/usr/bin/other-tool"}]}
            ]
        },
    }
    (claude / "settings.json").write_text(json.dumps(existing, indent=2))

    rc = _run(tmp_path)
    assert rc == 0

    settings = _read_settings(tmp_path)
    # Unrelated top-level keys preserved.
    assert settings["someOtherSetting"] is True
    assert settings["$schema"].endswith("claude-code-settings.json")
    # Pre-existing unrelated hook still present, ours appended.
    pre = settings["hooks"]["PreToolUse"]
    commands = [h["command"] for e in pre for h in e["hooks"]]
    assert "/usr/bin/other-tool" in commands
    assert FAKE_HOOK in commands
    # PostToolUse created with our entry.
    post = settings["hooks"]["PostToolUse"]
    assert any(
        h["command"] == FAKE_HOOK for e in post for h in e["hooks"]
    )


def test_init_is_idempotent(tmp_path, patched_binary, capsys):
    assert _run(tmp_path) == 0
    capsys.readouterr()  # clear

    # Second run must not duplicate entries and must report no-op.
    assert _run(tmp_path) == 0
    out = capsys.readouterr().out
    assert "already wired" in out.lower() or "nothing to do" in out.lower()

    settings = _read_settings(tmp_path)
    for event in ("PreToolUse", "PostToolUse", "SessionEnd"):
        cmds = [
            h["command"]
            for e in settings["hooks"][event]
            for h in e["hooks"]
        ]
        assert cmds.count(FAKE_HOOK) == 1


def test_init_upgrade_adds_session_end_to_existing_pre_post(
    tmp_path, patched_binary, capsys
):
    """v0.2.6 -> v0.2.6.1 upgrade: an operator already wired for Pre/Post
    re-runs init; SessionEnd is added, Pre/Post are left untouched (not
    duplicated), and the run is reported as a partial install."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": FAKE_HOOK}]}
            ],
            "PostToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": FAKE_HOOK}]}
            ],
        }
    }
    (claude / "settings.json").write_text(json.dumps(existing, indent=2))

    rc = _run(tmp_path)
    assert rc == 0

    settings = _read_settings(tmp_path)
    # SessionEnd now wired exactly once.
    se_cmds = [
        h["command"] for e in settings["hooks"]["SessionEnd"] for h in e["hooks"]
    ]
    assert se_cmds == [FAKE_HOOK]
    # Pre/Post untouched — still exactly one entry each, not duplicated.
    for event in ("PreToolUse", "PostToolUse"):
        cmds = [
            h["command"] for e in settings["hooks"][event] for h in e["hooks"]
        ]
        assert cmds.count(FAKE_HOOK) == 1

    out = capsys.readouterr().out
    assert "SessionEnd" in out
    assert "already present" in out  # Pre/Post reported as pre-existing


def test_init_errors_on_missing_binary(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: None)
    rc = _run(tmp_path)
    assert rc == 1
    assert "could not locate" in capsys.readouterr().err


def test_init_errors_on_nondirectory_path(tmp_path, patched_binary, capsys):
    bogus = tmp_path / "nope"
    rc = _run(bogus)
    assert rc == 1
    assert "not a directory" in capsys.readouterr().err


def test_init_refuses_malformed_settings(tmp_path, patched_binary, capsys):
    claude = tmp_path / ".claude"
    claude.mkdir()
    # Valid JSON, but an array rather than an object — triggers the
    # not-an-object guard (distinct from the unparseable-JSON guard).
    (claude / "settings.json").write_text("[1, 2, 3]")
    rc = _run(tmp_path)
    assert rc == 1
    assert "json object" in capsys.readouterr().err.lower()


def test_init_refuses_unparseable_settings(tmp_path, patched_binary, capsys):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{ not valid json")
    rc = _run(tmp_path)
    assert rc == 1
    assert "could not read existing" in capsys.readouterr().err.lower()


def test_resolve_hook_binary_prefers_interpreter_sibling(tmp_path, monkeypatch):
    """The hook belonging to the SAME install (sibling of sys.executable)
    must win over a different install found earlier on PATH — otherwise
    `init` can wire a hook of the wrong version (smoke-test finding)."""
    import shutil

    bindir = tmp_path / "bin"
    bindir.mkdir()
    sibling = bindir / "sentience-claude-code-hook"
    sibling.write_text("#!/bin/sh\n")
    sibling.chmod(0o755)

    monkeypatch.setattr(ux.sys, "executable", str(bindir / "python"))
    # shutil.which points at a DIFFERENT install — must be ignored.
    monkeypatch.setattr(shutil, "which", lambda name: "/other/install/bin/" + name)

    resolved = ux._resolve_hook_binary()
    assert resolved == str(sibling.resolve())


def test_resolve_hook_binary_does_not_follow_python_symlink(tmp_path, monkeypatch):
    """Regression: a venv's `python` is a symlink to the base interpreter.
    The resolver must look next to the (unresolved) venv python — where
    the hook sibling lives — NOT follow the symlink into the base
    Python's bin dir. Caught by the 0.2.5.3 TestPyPI smoke test."""
    import shutil

    base_bin = tmp_path / "base"
    base_bin.mkdir()
    real_python = base_bin / "python3.12"
    real_python.write_text("#!/bin/sh\n")
    real_python.chmod(0o755)

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    venv_python = venv_bin / "python3.12"
    venv_python.symlink_to(real_python)  # venv python → base python
    venv_hook = venv_bin / "sentience-claude-code-hook"
    venv_hook.write_text("#!/bin/sh\n")
    venv_hook.chmod(0o755)

    # sys.executable is the venv python symlink (as in a real venv).
    monkeypatch.setattr(ux.sys, "executable", str(venv_python))
    # A different install is on PATH — must be ignored.
    monkeypatch.setattr(shutil, "which", lambda name: "/global/bin/" + name)

    resolved = ux._resolve_hook_binary()
    assert resolved == str(venv_hook)  # the venv's own hook, not /global, not base/


def test_resolve_hook_binary_falls_back_to_which(tmp_path, monkeypatch):
    """When no sibling exists next to the interpreter, fall back to PATH."""
    import shutil

    empty_bin = tmp_path / "empty"
    empty_bin.mkdir()
    monkeypatch.setattr(ux.sys, "executable", str(empty_bin / "python"))
    monkeypatch.setattr(shutil, "which", lambda name: "/somewhere/bin/" + name)

    resolved = ux._resolve_hook_binary()
    assert resolved == "/somewhere/bin/sentience-claude-code-hook"
