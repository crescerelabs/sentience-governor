"""Plan §12 tests 39–45, 47–54, 56–58 — the §3.5 transition matrix,
exercised through the full engine (`converge`) against a real filesystem.

Projects here sit OUTSIDE any git repository, so the resolved local file is
the starting directory's `.claude/settings.local.json` (§6.2 exception),
keeping paths deterministic.
"""

import json

import pytest

from sentience_governor.cli import hook_config as hc

from .conftest import make_exec, managed_entry, read_json, write_json


DEAD = "/nonexistent/bin/sentience-claude-code-hook"


def shared_doc_one(entry):
    return {"hooks": {"PreToolUse": [entry]}}


def assert_canonical(local_path, binary):
    doc = read_json(local_path)
    for ev in hc.GOVERNED_EVENTS:
        assert doc["hooks"][ev] == [managed_entry(binary)]


# ---------------------------------------------------------------------------
# 39–45
# ---------------------------------------------------------------------------

def test_39_motivating_incident_dead_shared_no_local(
    project, shared_path, local_path, binary, seam, capsys, no_tty
):
    """§12 test 39: dead MANAGED shared + no local → the seam CREATES the
    canonical local file, emits ONE stderr line (never TTY-suppressed), and
    `settings.json` is byte-identical."""
    write_json(shared_path, shared_doc_one(managed_entry(DEAD)))
    before = shared_path.read_bytes()

    res = seam()
    hc_emit = hc._emit_seam_output
    hc_emit(res)

    assert res.outcome == hc.CREATED
    assert_canonical(local_path, binary)
    assert shared_path.read_bytes() == before
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "created" in err and str(local_path) in err
    assert "not for commit" in err


def test_40_healthy_shared_equal_no_local_is_silent_noop(
    project, shared_path, local_path, binary, seam, capsys, tty
):
    """§12 test 40 (rule 2): live shared hook equal to canonical + no local →
    NO-OP. No local file is created and nothing is printed."""
    write_json(shared_path, shared_doc_one(managed_entry(binary)))
    res = seam()
    hc._emit_seam_output(res)
    assert res.outcome == hc.NOOP
    assert not local_path.exists()
    out = capsys.readouterr()
    assert out.err == "" and out.out == ""


def test_41_live_conflicting_shared_blocks_creation(
    project, shared_path, local_path, binary, other_binary, seam, capsys, tty
):
    """§12 test 41 (I13, rule 3): a live shared hook differing from canonical
    blocks; the seam writes NOTHING and warns — never a second live handler."""
    write_json(shared_path, shared_doc_one(managed_entry(other_binary)))
    res = seam()
    hc._emit_seam_output(res)
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Sentience" in err


def test_42_ambiguous_shared_blocks(
    project, shared_path, local_path, binary, seam, capsys, tty
):
    """§12 test 42: AMBIGUOUS shared entry → blocks; warns; no local
    creation. (Resolvability is never consulted; see test 43.)"""
    wrapper = {"matcher": "", "hooks": [{
        "type": "command",
        "command": f"sh -c '{binary} --flag'",
    }]}
    write_json(shared_path, shared_doc_one(wrapper))
    res = seam()
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_43_ambiguous_shared_with_dead_paths_still_blocks(
    project, shared_path, local_path, seam
):
    """§12 test 43 (gate-4 b1, reversed at 4.10): an AMBIGUOUS entry whose
    referenced paths do NOT verify still blocks — AMBIGUOUS is LIVE by fiat;
    deadness is not provable. Doubles as the guard against reintroducing
    AMBIGUOUS liveness verification."""
    wrapper = {"matcher": "", "hooks": [{
        "type": "command",
        "command": "sh -c '/nowhere/at/all/sentience-claude-code-hook'",
    }]}
    write_json(shared_path, shared_doc_one(wrapper))
    res = seam()
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_44_live_conflicting_shared_under_init(
    project, shared_path, local_path, binary, other_binary, monkeypatch,
    capsys, isolated_home
):
    """§12 test 44: under explicit `init` the same state exits non-zero,
    writes nothing, and prints the team-coordination message (§8.3)."""
    import argparse
    from sentience_governor.cli import ux

    write_json(shared_path, shared_doc_one(managed_entry(other_binary)))
    before = shared_path.read_bytes()
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: binary)
    rc = ux.run_init_claude_code(argparse.Namespace(
        path=str(project), no_skills=True, project=False, force=False))
    assert rc != 0
    assert not local_path.exists()
    assert shared_path.read_bytes() == before
    err = capsys.readouterr().err
    assert "shared with your team" in err
    assert "Coordinate" in err


def test_45_healthy_shared_equal_plus_stale_local_converges(
    project, shared_path, local_path, binary, seam
):
    """§12 test 45: live shared equal + STALE local → the local file
    converges; the result equals the shared command (dedup applies)."""
    write_json(shared_path, shared_doc_one(managed_entry(binary)))
    write_json(local_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    res = seam()
    assert res.outcome == hc.UPDATED
    assert_canonical(local_path, binary)


# ---------------------------------------------------------------------------
# 47–54
# ---------------------------------------------------------------------------

def test_47_python_dash_m_form_is_ambiguous_and_blocks(
    project, shared_path, local_path, seam
):
    """§12 test 47: `<py> -m sentience_governor.wrapper.claude_code_hook` is
    a working Sentience handler; it classifies AMBIGUOUS and blocks (I13)."""
    entry = {"matcher": "", "hooks": [{
        "type": "command",
        "command": "/usr/bin/python3 -m sentience_governor.wrapper.claude_code_hook",
    }]}
    assert hc.classify_entry(entry, posix=True) == "ambiguous"
    write_json(shared_path, shared_doc_one(entry))
    res = seam()
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_48_sh_dash_c_payload_is_ambiguous_and_blocks(
    project, shared_path, local_path, seam
):
    """§12 test 48: `sh -c '/path/sentience-claude-code-hook --flag'` —
    AMBIGUOUS; convergence blocked."""
    entry = {"matcher": "", "hooks": [{
        "type": "command",
        "command": "sh -c '/path/sentience-claude-code-hook --flag'",
    }]}
    assert hc.classify_entry(entry, posix=True) == "ambiguous"
    write_json(shared_path, shared_doc_one(entry))
    res = seam()
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_49_s2_l3_warns_instead_of_silent_noop(
    project, shared_path, local_path, binary, seam
):
    """§12 test 49 (gate finding 2): healthy shared equal + AMBIGUOUS local →
    AMBIGUOUS_WARN, not a silent rule-2 NOOP — step 8 precedes step 10."""
    write_json(shared_path, shared_doc_one(managed_entry(binary)))
    write_json(local_path, {"hooks": {"PreToolUse": [{
        "matcher": "", "hooks": [{
            "type": "command", "command": f"{binary} --verbose"}]}]}})
    res = seam()
    assert res.outcome == hc.AMBIGUOUS_LOCAL


def test_50_s3_l3_and_s5_l3_report_ambiguous_local_only(
    project, shared_path, local_path, binary, other_binary, seam, capsys, tty
):
    """§12 test 50 (gate-3 nb7): with an ambiguous LOCAL entry present,
    AMBIGUOUS_WARN is the only report — step 8 stops before the shared
    conflict is evaluated, matching the corrected §3.5 cells."""
    ambiguous_local = {"hooks": {"PostToolUse": [{
        "matcher": "", "hooks": [{
            "type": "command", "command": f"{binary} --wrapped"}]}]}}
    for shared_entry in (
        managed_entry(other_binary),                       # S3
        {"matcher": "", "hooks": [{"type": "command",
                                   "command": f"sh -c '{binary}'"}]},  # S5
    ):
        if shared_path.exists():
            shared_path.unlink()
        write_json(shared_path, shared_doc_one(shared_entry))
        write_json(local_path, ambiguous_local)
        res = seam()
        hc._emit_seam_output(res)
        assert res.outcome == hc.AMBIGUOUS_LOCAL
        err = capsys.readouterr().err
        assert err.count("\n") == 1
        assert "modified Sentience hook" in err


def test_52_spaced_live_managed_path_is_live_and_blocks(
    tmp_path, project, shared_path, local_path, seam
):
    """§12 test 52 (gate-2 finding 1): a live MANAGED entry whose path
    contains SPACES is verified as one whole path — LIVE — and, differing
    from canonical, blocks. Tokenizing would have fragmented it to 'dead'."""
    spaced = make_exec(tmp_path / "My Projects" / "venv (2)" / "bin"
                       / hc.HOOK_BASENAME)
    write_json(shared_path, shared_doc_one(managed_entry(spaced)))
    res = seam()
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_53_mixed_live_entries_equal_never_masks_differing(
    project, shared_path, local_path, binary, other_binary, seam
):
    """§12 test 53 (gate-2 finding 3): one MANAGED-equal AND one
    MANAGED-differing live entry in the SAME event → SHARED_CONFLICT, not
    NOOP. The conflict rule quantifies over the SET."""
    write_json(shared_path, {"hooks": {"PreToolUse": [
        managed_entry(binary), managed_entry(other_binary)]}})
    res = seam()
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_54_s2_l0_init_creates_seam_noops(
    project, shared_path, local_path, binary, seam, init
):
    """§12 test 54 (gate-2 finding 2): on S2×L0 the seam NOOPs (rule 2), but
    explicit `init` CREATES the local canonical file — identical to the live
    shared command, so Claude Code de-duplicates."""
    write_json(shared_path, shared_doc_one(managed_entry(binary)))
    assert seam().outcome == hc.NOOP
    assert not local_path.exists()
    res = init()
    assert res.outcome == hc.CREATED
    assert_canonical(local_path, binary)


# ---------------------------------------------------------------------------
# 56–58
# ---------------------------------------------------------------------------

def test_56_tilde_form_live_after_expanduser_blocks(
    project, shared_path, local_path, binary, seam, monkeypatch, tmp_path
):
    """§12 test 56 (gate-3 blocker 1): a hand-authored tilde-form MANAGED
    entry whose target exists after expansion is LIVE; differing from
    canonical, it blocks — no local creation."""
    home = tmp_path / "tilde-home"
    monkeypatch.setenv("HOME", str(home))
    make_exec(home / ".local" / "bin" / hc.HOOK_BASENAME)
    write_json(shared_path, shared_doc_one(
        managed_entry("~/.local/bin/sentience-claude-code-hook")))
    res = seam()
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_57_bare_name_is_live_by_fiat_and_blocks(
    project, shared_path, local_path, seam
):
    """§12 test 57 (gate-3 blocker 1): a bare-name MANAGED entry may resolve
    via PATH in the hook's shell and cannot be proven dead — LIVE by fiat;
    blocks; no local creation."""
    write_json(shared_path, shared_doc_one(
        managed_entry("sentience-claude-code-hook")))
    res = seam()
    assert res.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_58_init_on_malformed_local_exits_nonzero(
    project, local_path, binary, monkeypatch, capsys, isolated_home
):
    """§12 test 58 (gate-3 blocker 2): `init` on a malformed
    `settings.local.json` with no shared file exits non-zero with the reason
    and mutates nothing — never a silent zero-exit NOOP."""
    import argparse
    from sentience_governor.cli import ux

    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text("{ this is not json")
    before = local_path.read_bytes()
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: binary)
    rc = ux.run_init_claude_code(argparse.Namespace(
        path=str(project), no_skills=True, project=False, force=False))
    assert rc != 0
    assert local_path.read_bytes() == before
    assert "could not read" in capsys.readouterr().err
