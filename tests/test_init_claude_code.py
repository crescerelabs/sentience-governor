"""`sentience init claude-code` — canonical convergence (v0.3.0.3).

From v0.3.0.3 the hook binding lives in the machine-local
`.claude/settings.local.json`; the team-shared `.claude/settings.json` is
READ-ONLY migration evidence. `init` is `converge(target,
may_create_without_evidence=True)`: it creates canonical configuration on a
clean project, brings historical stale/partial/duplicate configuration
forward, and never writes the shared file. The `_resolve_hook_binary`
resolution-order tests at the bottom predate this release and are unchanged.
"""

import argparse
import json
from pathlib import Path

import pytest

from sentience_governor.cli import ux
from sentience_governor.cli import hook_config as hc


@pytest.fixture
def fake_binary(tmp_path, monkeypatch):
    """A deterministic, REAL, executable hook binary: v0.3.0.3 init verifies
    the resolved binary before writing (A11), so a bare fake path fails."""
    p = tmp_path / "fakebin" / "sentience-claude-code-hook"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: str(p))
    return str(p)


def _run(path: Path) -> int:
    # Hook-focused tests opt out of skill install (skills get their own
    # suite); keeps these from touching the real ~/.claude/skills/.
    args = argparse.Namespace(
        path=str(path), no_skills=True, project=False, force=False
    )
    return ux.run_init_claude_code(args)


def _read_local(project: Path) -> dict:
    return json.loads(
        (project / ".claude" / "settings.local.json").read_text()
    )


def test_init_creates_local_settings_when_absent(tmp_path, fake_binary, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    rc = _run(proj)
    assert rc == 0

    settings = _read_local(proj)
    for event in ("PreToolUse", "PostToolUse", "SessionEnd"):
        entries = settings["hooks"][event]
        assert len(entries) == 1
        assert entries[0]["hooks"][0]["command"] == fake_binary
        assert entries[0]["matcher"] == ""
    # The shared team file is never created by init.
    assert not (proj / ".claude" / "settings.json").exists()

    out = capsys.readouterr().out
    assert "hook configured" in out
    assert "settings.local.json" in out
    assert "not for commit" in out
    assert "sentience status" in out


def test_init_leaves_shared_file_untouched_and_preserves_foreign_hooks(
    tmp_path, fake_binary
):
    """A shared settings.json with foreign hooks is READ-ONLY evidence:
    byte-identical after init; our binding lands in settings.local.json."""
    proj = tmp_path / "proj"
    claude = proj / ".claude"
    claude.mkdir(parents=True)
    existing = {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "someOtherSetting": True,
        "hooks": {
            "PreToolUse": [
                {"matcher": "", "hooks": [
                    {"type": "command", "command": "/usr/bin/other-tool"}]}
            ]
        },
    }
    shared = claude / "settings.json"
    shared.write_text(json.dumps(existing, indent=2))
    before = shared.read_bytes()

    rc = _run(proj)
    assert rc == 0
    assert shared.read_bytes() == before  # never written

    local = _read_local(proj)
    for event in ("PreToolUse", "PostToolUse", "SessionEnd"):
        cmds = [h["command"] for e in local["hooks"][event] for h in e["hooks"]]
        assert cmds == [fake_binary]


def test_init_is_idempotent(tmp_path, fake_binary, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    assert _run(proj) == 0
    first = (proj / ".claude" / "settings.local.json").read_bytes()
    capsys.readouterr()  # clear

    # Second run: canonical is a fixed point — no write, reported as current.
    assert _run(proj) == 0
    assert (proj / ".claude" / "settings.local.json").read_bytes() == first
    out = capsys.readouterr().out
    assert "already current" in out


def test_init_converges_legacy_shared_pre_post_wiring(
    tmp_path, fake_binary, capsys
):
    """v0.2.6-era upgrade state: the operator's SHARED file carries dead
    Pre/Post entries from an old install. init leaves the shared file
    byte-identical (read-only evidence) and creates the full canonical
    machine-local configuration — all three events, current binary."""
    proj = tmp_path / "proj"
    claude = proj / ".claude"
    claude.mkdir(parents=True)
    dead = "/gone/bin/sentience-claude-code-hook"
    existing = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": dead}]}
            ],
            "PostToolUse": [
                {"matcher": "", "hooks": [{"type": "command", "command": dead}]}
            ],
        }
    }
    shared = claude / "settings.json"
    shared.write_text(json.dumps(existing, indent=2))
    before = shared.read_bytes()

    rc = _run(proj)
    assert rc == 0
    assert shared.read_bytes() == before

    local = _read_local(proj)
    for event in ("PreToolUse", "PostToolUse", "SessionEnd"):
        cmds = [h["command"] for e in local["hooks"][event] for h in e["hooks"]]
        assert cmds == [fake_binary]


def test_init_stale_local_converges_to_current_binary(tmp_path, fake_binary):
    """The motivating class: a local binding pointing at a removed install
    is brought forward to the running install's binary."""
    proj = tmp_path / "proj"
    claude = proj / ".claude"
    claude.mkdir(parents=True)
    stale = {
        "hooks": {
            ev: [{"matcher": "", "hooks": [{
                "type": "command",
                "command": "/gone/bin/sentience-claude-code-hook"}]}]
            for ev in ("PreToolUse", "PostToolUse", "SessionEnd")
        }
    }
    (claude / "settings.local.json").write_text(json.dumps(stale, indent=2))

    rc = _run(proj)
    assert rc == 0
    local = _read_local(proj)
    for event in ("PreToolUse", "PostToolUse", "SessionEnd"):
        cmds = [h["command"] for e in local["hooks"][event] for h in e["hooks"]]
        assert cmds == [fake_binary]


def test_init_errors_on_missing_binary(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: None)
    proj = tmp_path / "proj"
    proj.mkdir()
    rc = _run(proj)
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not locate" in err


def test_init_errors_on_nonexecutable_binary(tmp_path, monkeypatch, capsys):
    """A11: a resolved-but-non-executable binary is refused before any write."""
    p = tmp_path / "fakebin" / "sentience-claude-code-hook"
    p.parent.mkdir(parents=True)
    p.write_text("not executable")
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: str(p))
    proj = tmp_path / "proj"
    proj.mkdir()
    rc = _run(proj)
    assert rc == 1
    assert "not executable" in capsys.readouterr().err
    assert not (proj / ".claude").exists()


def test_init_errors_on_nondirectory_path(tmp_path, fake_binary, capsys):
    f = tmp_path / "afile"
    f.write_text("x")
    rc = _run(f)
    assert rc == 1
    assert "not a directory" in capsys.readouterr().err


def test_init_refuses_malformed_shared_settings(tmp_path, fake_binary, capsys):
    proj = tmp_path / "proj"
    claude = proj / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text(json.dumps(["not", "an", "object"]))
    rc = _run(proj)
    assert rc == 1
    assert "could not read" in capsys.readouterr().err
    assert not (claude / "settings.local.json").exists()


def test_init_refuses_unparseable_shared_settings(tmp_path, fake_binary, capsys):
    proj = tmp_path / "proj"
    claude = proj / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text("{ this is not json")
    rc = _run(proj)
    assert rc == 1
    assert "could not read" in capsys.readouterr().err
    assert not (claude / "settings.local.json").exists()


def test_init_refuses_modified_local_sentience_entry(
    tmp_path, fake_binary, capsys
):
    """An operator-customised Sentience-looking LOCAL entry is never guessed
    at: init reports it and exits non-zero without writing."""
    proj = tmp_path / "proj"
    claude = proj / ".claude"
    claude.mkdir(parents=True)
    doc = {"hooks": {"PreToolUse": [{
        "matcher": "", "hooks": [{
            "type": "command",
            "command": f"{fake_binary} --custom-flag"}]}]}}
    lp = claude / "settings.local.json"
    lp.write_text(json.dumps(doc, indent=2))
    before = lp.read_bytes()

    rc = _run(proj)
    assert rc == 1
    assert lp.read_bytes() == before
    err = capsys.readouterr().err
    assert "will not change it" in err


# ---------------------------------------------------------------------------
# _resolve_hook_binary resolution order (pre-v0.3.0.3 behaviour, unchanged)
# ---------------------------------------------------------------------------

def test_resolve_hook_binary_prefers_interpreter_sibling(tmp_path, monkeypatch):
    """The binary next to sys.executable wins over PATH."""
    import sys as _sys

    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    hook = bindir / "sentience-claude-code-hook"
    hook.write_text("#!/bin/sh\n")
    fake_python = bindir / "python"
    fake_python.write_text("#!/bin/sh\n")

    monkeypatch.setattr(_sys, "executable", str(fake_python))
    assert ux._resolve_hook_binary() == str(hook)


def test_resolve_hook_binary_does_not_follow_python_symlink(
    tmp_path, monkeypatch
):
    """A venv python is a symlink to the base interpreter; resolution must
    use the symlink's own directory, not the base Python's."""
    import sys as _sys

    base = tmp_path / "base" / "bin"
    base.mkdir(parents=True)
    real_python = base / "python"
    real_python.write_text("#!/bin/sh\n")

    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(real_python)
    hook = venv / "sentience-claude-code-hook"
    hook.write_text("#!/bin/sh\n")

    monkeypatch.setattr(_sys, "executable", str(venv / "python"))
    assert ux._resolve_hook_binary() == str(hook)


def test_resolve_hook_binary_falls_back_to_which(tmp_path, monkeypatch):
    import shutil as _shutil
    import sys as _sys

    empty = tmp_path / "nothing" / "bin"
    empty.mkdir(parents=True)
    monkeypatch.setattr(_sys, "executable", str(empty / "python"))

    onpath = tmp_path / "onpath" / "sentience-claude-code-hook"
    onpath.parent.mkdir(parents=True)
    onpath.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        _shutil, "which",
        lambda name: str(onpath) if name == "sentience-claude-code-hook" else None,
    )
    assert ux._resolve_hook_binary() == str(onpath.resolve())
