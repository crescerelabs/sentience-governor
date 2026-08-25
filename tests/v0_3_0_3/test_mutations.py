"""Plan §12 mutation tests 36–38, 46, 55, 59.

Each mutation disables ONE named rule (the §4.2 helpers exist precisely so
these tests target explicit conditions, not fall-through behaviour — A10) and
asserts that the corresponding guard test's property is then violated. The
monkeypatch context restores the rule automatically, and a follow-up assertion
proves the property holds again — the plan's plant → fail → restore → pass
protocol, executed in-process.
"""

from sentience_governor.cli import hook_config as hc

from .conftest import make_exec, managed_entry, write_json

DEAD = "/nonexistent/bin/sentience-claude-code-hook"


def test_36_removing_duplicate_collapse_breaks_test_4(monkeypatch):
    """§12 test 36: mutate step 11's collapse (keep managed duplicates) →
    test 4's exactly-one property is violated; restore → holds."""
    entries = [managed_entry(DEAD), managed_entry(DEAD)]
    local = {"hooks": {"PreToolUse": list(entries)}}

    real = hc._converge_event_entries

    def no_collapse(ents, binary, posix=None):
        return list(ents) + [hc.canonical_entry(binary)]  # append, no removal

    monkeypatch.setattr(hc, "_converge_event_entries", no_collapse)
    mutated = hc.plan_convergence(local, {}, "/b/sentience-claude-code-hook",
                                  False, True, posix=True)
    managed_count = sum(
        1 for e in mutated.new_local["hooks"]["PreToolUse"]
        if hc.classify_entry(e, posix=True) == "managed")
    assert managed_count > 1  # test 4's property now FAILS under the mutation

    monkeypatch.setattr(hc, "_converge_event_entries", real)
    fixed = hc.plan_convergence(local, {}, "/b/sentience-claude-code-hook",
                                False, True, posix=True)
    assert len(fixed.new_local["hooks"]["PreToolUse"]) == 1


def test_37_removing_evidence_guard_breaks_test_8(monkeypatch):
    """§12 test 37: mutate step 5's evidence gate (always proceed) → the
    seam configures a zero-evidence project, violating test 8 / I6."""
    monkeypatch.setattr(hc, "_evidence_gate", lambda *_: True)
    mutated = hc.plan_convergence({}, {}, "/b/sentience-claude-code-hook",
                                  False, True, posix=True)
    assert mutated.outcome == "plan"  # test 8 expects NOOP → it now fails

    monkeypatch.undo()
    fixed = hc.plan_convergence({}, {}, "/b/sentience-claude-code-hook",
                                False, True, posix=True)
    assert fixed.outcome == hc.NOOP


def test_38_removing_lost_update_compare_breaks_test_22(
    project, local_path, binary, seam, monkeypatch
):
    """§12 test 38: mutate the compare-before-replace (snapshots always
    'equal') → the other writer's update is silently overwritten, violating
    test 22 / I12."""
    write_json(local_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    other = b'{"hooks": {}, "otherWriter": true}\n'

    def race_reread(path):
        local_path.write_bytes(other)
        return other

    monkeypatch.setattr(hc, "_reread_for_compare", race_reread)
    monkeypatch.setattr(hc, "_snapshots_equal", lambda a, b: True)
    mutated = seam()
    assert mutated.outcome == hc.UPDATED          # write went through…
    assert local_path.read_bytes() != other       # …clobbering the other writer

    monkeypatch.undo()
    write_json(local_path, {"hooks": {"PreToolUse": [managed_entry(DEAD)]}})
    monkeypatch.setattr(hc, "_reread_for_compare", race_reread)
    fixed = seam()
    assert fixed.outcome == hc.WRITE_CONFLICT
    assert local_path.read_bytes() == other


def test_46_removing_shared_live_guard_breaks_test_41(
    project, shared_path, local_path, binary, other_binary, seam, monkeypatch
):
    """§12 test 46: mutate step 9's conflict rule (never conflict) → the
    seam creates a second live handler under a live differing shared entry,
    violating test 41 / I13."""
    write_json(shared_path, {"hooks": {"PreToolUse": [
        managed_entry(other_binary)]}})

    monkeypatch.setattr(hc, "_shared_live_conflict", lambda *_: False)
    mutated = seam()
    assert mutated.outcome == hc.CREATED   # two live handlers now exist
    assert local_path.exists()

    monkeypatch.undo()
    local_path.unlink()
    fixed = seam()
    assert fixed.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_55_tokenizing_managed_liveness_breaks_test_52(
    tmp_path, project, shared_path, local_path, binary, seam, monkeypatch
):
    """§12 test 55: replace step 9's per-class rule with tokenized-for-both →
    a live spaced-path MANAGED entry fragments to 'dead' and the block is
    missed, violating test 52. Restore → the block holds."""
    spaced = make_exec(tmp_path / "My Projects" / "bin" / hc.HOOK_BASENAME)
    write_json(shared_path, {"hooks": {"PreToolUse": [managed_entry(spaced)]}})

    def tokenized(command, posix=None):
        return any(hc._verify_path(tok, posix) for tok in command.split())

    monkeypatch.setattr(hc, "managed_entry_live", tokenized)
    mutated = seam()
    assert mutated.outcome == hc.CREATED   # conflict missed → second handler
    assert local_path.exists()

    monkeypatch.undo()
    local_path.unlink()
    fixed = seam()
    assert fixed.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()


def test_59_removing_live_by_fiat_breaks_test_57(
    project, shared_path, local_path, seam, monkeypatch
):
    """§12 test 59: mutate the non-absolute-LIVE-by-fiat rule (non-absolute
    → dead) → a bare-name entry is treated dead and the seam creates a
    potentially second live handler, violating test 57. Restore → blocks."""
    write_json(shared_path, {"hooks": {"PreToolUse": [
        managed_entry("sentience-claude-code-hook")]}})

    import os

    def no_fiat(command, posix=None):
        expanded = os.path.expanduser(command)
        if not os.path.isabs(expanded):
            return False               # the mutation: unprovable → "dead"
        return hc._verify_path(expanded, posix)

    monkeypatch.setattr(hc, "managed_entry_live", no_fiat)
    mutated = seam()
    assert mutated.outcome == hc.CREATED
    assert local_path.exists()

    monkeypatch.undo()
    local_path.unlink()
    fixed = seam()
    assert fixed.outcome == hc.SHARED_CONFLICT
    assert not local_path.exists()
