"""Plan §12 tests 16–31, 33–35, 51 — CLI-level and failure-path behaviour."""

import argparse
import json
import os
import stat
import sys

import pytest

from sentience_governor.cli import hook_config as hc

from .conftest import make_exec, managed_entry, read_json, write_json

DEAD = "/nonexistent/bin/sentience-claude-code-hook"


def stale_local(local_path):
    write_json(local_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})


# ---------------------------------------------------------------------------
# 16 — the seam inside a real CLI command
# ---------------------------------------------------------------------------

def test_16_pulse_converges_existing_local_silently(
    project, local_path, binary, monkeypatch, capsys, isolated_home, tmp_path
):
    """§12 test 16: a real `sentience pulse` invocation converges a stale
    existing local file; the command's own output is produced normally and
    NOTHING about the migration appears on stdout or stderr (success while
    converging an existing file is silent; warnings are TTY-gated off)."""
    from sentience_governor.cli import ux

    stale_local(local_path)
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: binary)
    monkeypatch.setattr(hc, "_stderr_isatty", lambda: False)
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["sentience", "pulse", "--no-prompt"])

    try:
        ux.main()
    except SystemExit:
        pass

    doc = read_json(local_path)
    for ev in hc.GOVERNED_EVENTS:
        assert doc["hooks"][ev] == [managed_entry(binary)]
    captured = capsys.readouterr()
    assert "Sentience:" not in captured.err
    assert "settings.local.json" not in captured.out


# ---------------------------------------------------------------------------
# 17–21 — failure paths, evidence-gated
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.getuid() == 0, reason="permissions are moot as root")
def test_17_unreadable_with_evidence_warns_and_mutates_nothing(
    project, shared_path, local_path, seam, tty, capsys
):
    """§12 test 17: unreadable settings + evidence elsewhere → one warning,
    no mutation, and the invoked command is unaffected."""
    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    stale_local(local_path)
    local_path.chmod(0)
    try:
        res = seam()
        hc._emit_seam_output(res)
        assert res.outcome == hc.UNREADABLE
        err = capsys.readouterr().err
        assert err.count("\n") == 1 and "could not read" in err
    finally:
        local_path.chmod(0o644)


def test_18_malformed_with_evidence_warns_and_mutates_nothing(
    project, shared_path, local_path, seam, tty, capsys
):
    """§12 test 18: malformed local + evidence in shared → one warning,
    nothing mutated."""
    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text("{ nope")
    before = local_path.read_bytes()
    res = seam()
    hc._emit_seam_output(res)
    assert res.outcome == hc.MALFORMED
    assert local_path.read_bytes() == before
    assert "could not read" in capsys.readouterr().err


@pytest.mark.skipif(os.getuid() == 0, reason="permissions are moot as root")
def test_19_unwritable_warns_and_command_continues(
    project, local_path, seam, tty, capsys
):
    """§12 test 19: an unwritable target directory → one warning; the
    failure is not hidden."""
    stale_local(local_path)
    local_path.parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        res = seam()
        hc._emit_seam_output(res)
        assert res.outcome in (hc.UNWRITABLE, hc.WRITE_CONFLICT)
        assert "could not update" in capsys.readouterr().err
    finally:
        local_path.parent.chmod(0o755)


def test_20_no_binary_with_evidence_warns(
    project, shared_path, local_path, tty, capsys
):
    """§12 test 20 (F6): evidence present but the running install cannot
    resolve its own hook binary → one warning. A project that appears
    configured while capture is dead is the defect this patch exists for."""
    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    res = hc.converge(project, caller="seam", binary_resolver=lambda: None)
    hc._emit_seam_output(res)
    assert res.outcome == hc.NO_BINARY
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "hook binary" in err


def test_21_no_binary_without_evidence_is_silent(
    project, tty, capsys
):
    """§12 test 21 (finding 4): no evidence → silence, even when the binary
    is unresolvable. Nothing to report."""
    res = hc.converge(project, caller="seam", binary_resolver=lambda: None)
    hc._emit_seam_output(res)
    assert res.outcome == hc.NOOP
    out = capsys.readouterr()
    assert out.err == "" and out.out == ""


# ---------------------------------------------------------------------------
# 22–25 — write safety and the read-only shared file
# ---------------------------------------------------------------------------

def test_22_lost_update_aborts_and_preserves_other_writer(
    project, local_path, seam, monkeypatch, tty, capsys
):
    """§12 test 22 (I12, C5): the file changes between our read and our
    replace → the write ABORTS, the other writer's content is retained, and a
    warning is emitted."""
    stale_local(local_path)
    other = b'{"hooks": {}, "otherWriter": true}\n'

    def race_reread(path):
        local_path.write_bytes(other)   # the concurrent writer lands here
        return other

    monkeypatch.setattr(hc, "_reread_for_compare", race_reread)
    res = seam()
    hc._emit_seam_output(res)
    assert res.outcome == hc.WRITE_CONFLICT
    assert local_path.read_bytes() == other
    assert "could not update" in capsys.readouterr().err


def test_23_torn_write_leaves_original_parseable(
    project, local_path, seam, monkeypatch
):
    """§12 test 23: an interrupted apply (fsync raises) leaves the original
    file complete and parseable; the temp file is cleaned up."""
    stale_local(local_path)
    before = read_json(local_path)

    def boom(fd):
        raise OSError("simulated device failure")

    monkeypatch.setattr(hc.os, "fsync", boom)
    res = seam()
    assert res.outcome == hc.UNWRITABLE
    assert read_json(local_path) == before
    leftovers = [p for p in local_path.parent.iterdir()
                 if p.name.startswith(".sentience-settings-")]
    assert leftovers == []


def test_24_seam_never_writes_shared(
    project, shared_path, local_path, seam
):
    """§12 test 24 (I11): `settings.json` is byte-identical after any seam
    command — it is read-only migration evidence."""
    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    before = shared_path.read_bytes()
    res = seam()
    assert res.outcome == hc.CREATED
    assert shared_path.read_bytes() == before


def test_25_no_ping_pong_between_teammates(
    project, shared_path, local_path, tmp_path
):
    """§12 test 25 (F1): two runs simulating different teammate installs —
    `settings.json` is unchanged BOTH times; each teammate converges only
    their machine-local file."""
    bin_a = make_exec(tmp_path / "teammate-a" / hc.HOOK_BASENAME)
    bin_b = make_exec(tmp_path / "teammate-b" / hc.HOOK_BASENAME)
    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    before = shared_path.read_bytes()

    res_a = hc.converge(project, caller="seam", binary_resolver=lambda: bin_a)
    assert res_a.outcome == hc.CREATED
    assert shared_path.read_bytes() == before

    res_b = hc.converge(project, caller="seam", binary_resolver=lambda: bin_b)
    assert res_b.outcome == hc.UPDATED
    assert shared_path.read_bytes() == before
    doc = read_json(local_path)
    assert doc["hooks"]["PreToolUse"] == [managed_entry(bin_b)]


# ---------------------------------------------------------------------------
# 26–27 — local-file resolution
# ---------------------------------------------------------------------------

def test_26_nested_subdirectory_resolves_to_repo_root(tmp_path, binary):
    """§12 test 26 (§6.2): inside a git repository, the local file resolves
    at the repository root — converging from a nested subdirectory creates
    the file THERE."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    sub = root / "docs" / "strategy"
    sub.mkdir(parents=True)
    write_json(sub / ".claude" / "settings.json",
               {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})

    resolved = hc.resolve_local_settings_path(sub)
    assert resolved == root / ".claude" / "settings.local.json"

    res = hc.converge(sub, caller="seam", binary_resolver=lambda: binary)
    assert res.outcome == hc.CREATED
    assert (root / ".claude" / "settings.local.json").is_file()


def test_27_outside_git_repo_stays_in_starting_directory(tmp_path):
    """§12 test 27 (§6.2 exception): outside a git repository the local file
    stays in the starting directory."""
    p = tmp_path / "plain"
    p.mkdir()
    assert hc.resolve_local_settings_path(p) == \
        p / ".claude" / "settings.local.json"


# ---------------------------------------------------------------------------
# 28–31
# ---------------------------------------------------------------------------

def test_28_explicit_init_target_never_migrates_cwd(
    tmp_path, binary, monkeypatch, isolated_home, capsys
):
    """§12 test 28 (I9): `sentience init claude-code /repo/B` run from a
    stale, migratable /repo/A converges ONLY B; A is untouched."""
    from sentience_governor.cli import ux

    proj_a = tmp_path / "proj-a"
    proj_a.mkdir()
    write_json(proj_a / ".claude" / "settings.json",
               {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    a_shared_before = (proj_a / ".claude" / "settings.json").read_bytes()
    proj_b = tmp_path / "proj-b"
    proj_b.mkdir()

    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: binary)
    monkeypatch.chdir(proj_a)
    monkeypatch.setattr(sys, "argv", [
        "sentience", "init", "claude-code", str(proj_b), "--no-skills"])
    rc = ux.main()
    assert rc == 0

    assert (proj_b / ".claude" / "settings.local.json").is_file()
    assert not (proj_a / ".claude" / "settings.local.json").exists()
    assert (proj_a / ".claude" / "settings.json").read_bytes() == a_shared_before


def test_29_bare_sentience_mutates_nothing(
    project, shared_path, local_path, binary, monkeypatch, isolated_home,
    capsys
):
    """§12 test 29 (F9): bare `sentience` is a help gesture — it prints the
    guide and mutates NOTHING, even in a stale, migratable project."""
    from sentience_governor.cli import ux

    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: binary)
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["sentience"])
    rc = ux.main()
    assert rc == 0
    assert not local_path.exists()
    assert "sentience" in capsys.readouterr().out.lower()


def test_30_sentience_cli_viewer_mutates_nothing(
    project, shared_path, local_path, monkeypatch, capsys, tmp_path
):
    """§12 test 30: `sentience-cli` (the trace viewer) carries NO seam — its
    path argument is a trace file, not a project."""
    from sentience_governor.cli import viewer

    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    trace = tmp_path / "empty-trace.jsonl"
    trace.write_text("")
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "argv", ["sentience-cli", str(trace)])
    try:
        viewer.main()
    except SystemExit:
        pass
    assert not local_path.exists()


def test_31_init_already_canonical_still_refreshes_skills(
    project, local_path, binary, monkeypatch, isolated_home, capsys
):
    """§12 test 31 (A9): `init` on an already-canonical project makes no
    hooks write but still refreshes skills — a re-run is NOT a total no-op."""
    import argparse
    from sentience_governor.cli import ux

    write_json(local_path, {"hooks": {
        ev: [managed_entry(binary)] for ev in hc.GOVERNED_EVENTS}})
    before = local_path.read_bytes()
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: binary)
    rc = ux.run_init_claude_code(argparse.Namespace(
        path=str(project), no_skills=False, project=False, force=False))
    assert rc == 0
    assert local_path.read_bytes() == before
    assert "already current" in capsys.readouterr().out
    skills_root = isolated_home / ".claude" / "skills"
    installed = list(skills_root.glob("sentience-*/SKILL.md"))
    assert len(installed) >= 6


# ---------------------------------------------------------------------------
# 33–35, 51
# ---------------------------------------------------------------------------

def test_33_install_and_upgrade_converge_deep_equal(
    tmp_path, binary, monkeypatch, isolated_home
):
    """§12 test 33 (I10): a project wired fresh by `init` and a project
    converged from the pre-v0.2.6.1 shape end deep-equal in their governed
    events — install and upgrade are one mechanism."""
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    res_fresh = hc.converge(fresh, caller="init",
                            binary_resolver=lambda: binary)
    assert res_fresh.outcome == hc.CREATED

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    write_json(legacy / ".claude" / "settings.json", {"hooks": {
        "PreToolUse": [managed_entry(DEAD)],
        "PostToolUse": [managed_entry(DEAD)],
    }})
    res_legacy = hc.converge(legacy, caller="seam",
                             binary_resolver=lambda: binary)
    assert res_legacy.outcome == hc.CREATED

    fresh_doc = read_json(fresh / ".claude" / "settings.local.json")
    legacy_doc = read_json(legacy / ".claude" / "settings.local.json")
    assert {ev: fresh_doc["hooks"][ev] for ev in hc.GOVERNED_EVENTS} == \
           {ev: legacy_doc["hooks"][ev] for ev in hc.GOVERNED_EVENTS}


def test_34_non_posix_miscased_exe_is_managed():
    """§12 test 34 (F11): on non-POSIX the predicate is case-insensitive —
    a miscased `.EXE` self-entry classifies MANAGED, not FOREIGN (which
    would lead `init` to append a duplicate live hook)."""
    entry = hc.canonical_entry("C:/tools/Sentience-Claude-Code-Hook.EXE")
    assert hc.classify_entry(entry, posix=False) == "managed"
    # POSIX comparison stays exact.
    assert hc.classify_entry(entry, posix=True) == "ambiguous"


def test_35_warnings_gated_by_tty(
    project, shared_path, local_path, binary, other_binary, seam,
    monkeypatch, capsys
):
    """§12 test 35 (F5): the same warning-producing state emits nothing when
    stderr is not a TTY, and exactly one line when it is."""
    write_json(shared_path, {"hooks": {"PreToolUse": [
        managed_entry(other_binary)]}})

    monkeypatch.setattr(hc, "_stderr_isatty", lambda: False)
    hc._emit_seam_output(seam())
    assert capsys.readouterr().err == ""

    monkeypatch.setattr(hc, "_stderr_isatty", lambda: True)
    hc._emit_seam_output(seam())
    assert capsys.readouterr().err.count("\n") == 1


def test_51_creation_line_never_tty_suppressed(
    project, shared_path, local_path, binary, seam, no_tty, capsys
):
    """§12 test 51 (gate finding 4): the file-creation line is emitted even
    when stderr is NOT a TTY — a new git-visible file must be attributed."""
    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    res = seam()
    hc._emit_seam_output(res)
    assert res.outcome == hc.CREATED
    err = capsys.readouterr().err
    assert "created" in err and "not for commit" in err
