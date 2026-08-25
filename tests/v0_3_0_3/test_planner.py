"""Plan §12 tests 1–15 and 32 — the pure planner and the classifier.

No filesystem access: `plan_convergence` is exercised directly on documents.
"""

import copy

from sentience_governor.cli import hook_config as hc

from .conftest import all_events, doc_with, managed_entry

BIN = "/install/bin/sentience-claude-code-hook"
STALE = "/old/bin/sentience-claude-code-hook"


def plan(local, shared=None, *, may_create=False, seam=True, binary=BIN):
    return hc.plan_convergence(
        local_doc=local, shared_doc=shared or {}, binary=binary,
        may_create_without_evidence=may_create, caller_is_seam=seam,
        posix=True,
    )


def canonical_doc():
    return all_events(lambda: managed_entry(BIN))


def test_01_canonical_is_a_fixed_point():
    """§12 test 1: canonical local → empty plan (I3)."""
    res = plan(canonical_doc())
    assert res.outcome == hc.NOOP


def test_02_partial_pre_0_2_6_1_adds_session_end_only():
    """§12 test 2: Pre+Post wired current, SessionEnd absent → the plan adds
    SessionEnd only; the existing events are untouched, no duplicates."""
    local = doc_with({
        "PreToolUse": [managed_entry(BIN)],
        "PostToolUse": [managed_entry(BIN)],
    })
    res = plan(local)
    assert res.outcome == "plan"
    new = res.new_local["hooks"]
    assert new["SessionEnd"] == [managed_entry(BIN)]
    for ev in ("PreToolUse", "PostToolUse"):
        assert new[ev] == [managed_entry(BIN)]
        assert len(new[ev]) == 1


def test_03_stale_all_events_rewrites_all_three():
    """§12 test 3."""
    res = plan(all_events(lambda: managed_entry(STALE)))
    assert res.outcome == "plan"
    for ev in hc.GOVERNED_EVENTS:
        assert res.new_local["hooks"][ev] == [managed_entry(BIN)]


def test_04_duplicates_collapse_to_exactly_one():
    """§12 test 4 (I2)."""
    local = doc_with({"PreToolUse": [managed_entry(STALE), managed_entry(BIN),
                                     managed_entry(STALE)]})
    res = plan(local)
    assert res.outcome == "plan"
    assert res.new_local["hooks"]["PreToolUse"] == [managed_entry(BIN)]


def test_05_stale_plus_duplicate_plus_missing_one_pass():
    """§12 test 5: one plan resolves all three defect classes; result is
    canonical."""
    local = doc_with({
        "PreToolUse": [managed_entry(STALE)],                    # stale
        "PostToolUse": [managed_entry(BIN), managed_entry(BIN)],  # duplicate
        # SessionEnd missing
    })
    res = plan(local)
    assert res.outcome == "plan"
    for ev in hc.GOVERNED_EVENTS:
        assert res.new_local["hooks"][ev] == [managed_entry(BIN)]


def test_06_one_stale_event_two_missing():
    """§12 test 6 (F7 totality): converge + fill in one pass."""
    local = doc_with({"PostToolUse": [managed_entry(STALE)]})
    res = plan(local)
    assert res.outcome == "plan"
    for ev in hc.GOVERNED_EVENTS:
        assert res.new_local["hooks"][ev] == [managed_entry(BIN)]


def test_07_apply_twice_second_is_empty():
    """§12 test 7 (I3): the planner's own output is a fixed point."""
    first = plan(all_events(lambda: managed_entry(STALE)))
    assert first.outcome == "plan"
    second = plan(first.new_local)
    assert second.outcome == hc.NOOP


def test_08_absent_no_evidence_seam_empty_plan():
    """§12 test 8 (I6): the seam never turns a project with zero Sentience
    evidence into a configured project."""
    res = plan({}, {}, may_create=False, seam=True)
    assert res.outcome == hc.NOOP
    assert res.evidence is False


def test_09_absent_with_init_plans_canonical_three():
    """§12 test 9: absent + init → canonical three."""
    res = plan({}, {}, may_create=True, seam=False)
    assert res.outcome == "plan"
    for ev in hc.GOVERNED_EVENTS:
        assert res.new_local["hooks"][ev] == [managed_entry(BIN)]


def test_10_mixed_outer_entry_is_ambiguous_and_untouched():
    """§12 test 10 (B1): our inner hook plus an operator inner hook in ONE
    outer entry → AMBIGUOUS; nothing is planned; the operator hook survives."""
    mixed = {"matcher": "", "hooks": [
        {"type": "command", "command": BIN},
        {"type": "command", "command": "/usr/bin/operator-tool"},
    ]}
    assert hc.classify_entry(mixed, posix=True) == "ambiguous"
    local = doc_with({"PreToolUse": [copy.deepcopy(mixed)]})
    res = plan(local)
    assert res.outcome == hc.AMBIGUOUS_LOCAL
    assert res.new_local is None
    assert local["hooks"]["PreToolUse"][0] == mixed  # untouched


def test_11_foreign_without_tokens_is_foreign_and_blocks_nothing():
    """§12 test 11 (restated at 4.10): a foreign command containing NONE of
    the three §5.3 tokens is FOREIGN and does not block convergence."""
    foreign = {"matcher": "*", "hooks": [
        {"type": "command", "command": "/usr/local/bin/linter --fix"}]}
    assert hc.classify_entry(foreign, posix=True) == "foreign"
    local = doc_with({"PreToolUse": [foreign, managed_entry(STALE)]})
    res = plan(local)
    assert res.outcome == "plan"  # the stale entry still converges


def test_12_foreign_entries_deep_equal_and_ordered():
    """§12 test 12 (I4): foreign entries survive deep-equal and in order."""
    f1 = {"matcher": "A", "hooks": [{"type": "command", "command": "/a"}]}
    f2 = {"matcher": "B", "hooks": [{"type": "command", "command": "/b"}]}
    local = doc_with({"PreToolUse": [f1, managed_entry(STALE), f2]})
    res = plan(local)
    assert res.outcome == "plan"
    out = res.new_local["hooks"]["PreToolUse"]
    assert [e for e in out if e in (f1, f2)] == [f1, f2]


def test_13_managed_at_index_one_stays_at_index_one():
    """§12 test 13 (A13): the canonical entry is inserted at the position of
    the first managed entry, preserving order relative to foreign hooks."""
    f1 = {"matcher": "A", "hooks": [{"type": "command", "command": "/a"}]}
    f2 = {"matcher": "B", "hooks": [{"type": "command", "command": "/b"}]}
    local = doc_with({"PreToolUse": [f1, managed_entry(STALE), f2]})
    res = plan(local)
    out = res.new_local["hooks"]["PreToolUse"]
    assert out == [f1, managed_entry(BIN), f2]


def test_14_predicate_covers_the_hook_entry_output_domain():
    """§12 test 14: `_hook_entry()`-shaped entries for plain / spaced /
    parenthesised paths all classify MANAGED. No `~` case: the resolver
    builds from sys.executable and cannot emit one (F11 — output-domain
    proof only; §5.2 still admits tilde strings structurally)."""
    for path in (
        "/simple/bin/sentience-claude-code-hook",
        "/My Projects/venv/bin/sentience-claude-code-hook",
        "/opt/venv (2)/bin/sentience-claude-code-hook",
    ):
        entry = hc.canonical_entry(path)
        assert hc.classify_entry(entry, posix=True) == "managed", path


def test_15_quoted_spaced_path_inside_wrapper_is_ambiguous():
    """§12 test 15 (finding 9): a wrapper carrying a quoted spaced path is
    AMBIGUOUS, not FOREIGN — the substring rule needs no tokenizer."""
    entry = {"matcher": "", "hooks": [{
        "type": "command",
        "command": "sh -c '/My Projects/bin/sentience-claude-code-hook --flag'",
    }]}
    assert hc.classify_entry(entry, posix=True) == "ambiguous"


def test_32_one_engine_both_flag_values():
    """§12 test 32 (I10): the SAME planner call, driven only by the two
    caller flags, produces the specified plans — no separate install/upgrade
    code path exists to diverge."""
    # absent + seam -> NOOP; absent + init -> canonical three.
    assert plan({}, {}, may_create=False, seam=True).outcome == hc.NOOP
    init_res = plan({}, {}, may_create=True, seam=False)
    assert init_res.outcome == "plan"
    # stale input: identical plans under both callers.
    stale = all_events(lambda: managed_entry(STALE))
    a = plan(copy.deepcopy(stale), {}, may_create=False, seam=True)
    b = plan(copy.deepcopy(stale), {}, may_create=True, seam=False)
    assert a.outcome == b.outcome == "plan"
    assert a.new_local == b.new_local
