"""Reader pipeline tests — v0.3.1 (CP1: plan §9 tests 1–17).

Every test maps to a numbered test in the locked v0.3.1 plan's §9 (the
single normative test list); the number appears in each docstring.

Isolation requirement (plan §9): every test runs under an isolated ``$HOME``
and ``$CLAUDE_CONFIG_DIR`` so nothing can touch the real user's settings,
transcripts, or first-run state.
"""

import hashlib
import json
import os
import shutil
import socket
import sys
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentience_governor import retro
from sentience_governor.analyze.renderers import render_scan
from sentience_governor.retro import (
    RootResolver,
    interpret_target,
    is_claude_config,
    scan,
    suppression_rule,
)

FIXTURES = Path(__file__).parent / "fixtures" / "retro"

# Fixed clock for window tests: finite windows are computed against this,
# never against the wall clock, so dated fixtures cannot rot.
FIXED_NOW = datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Isolated $HOME and $CLAUDE_CONFIG_DIR for every test (plan §9)."""
    home = tmp_path / "home"
    config = home / ".claude"
    (config / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


@pytest.fixture
def config_root(isolated_env):
    return isolated_env


def transcript_dir(config_root: Path, name: str = "-home-user-proj-a") -> Path:
    d = config_root / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_records(path: Path, records, *, final_newline: bool = True) -> None:
    text = "\n".join(json.dumps(r) for r in records)
    if final_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def activity(session_id, *, tools=(), cwd="/home/user/proj-a",
             timestamp="2026-08-24T12:00:00.000Z", **extra):
    """An assistant activity record carrying the given tool_use blocks."""
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "cwd": cwd,
        "message": {"role": "assistant",
                    "content": [dict(t, type="tool_use") for t in tools]},
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    record.update(extra)
    return record


def install_fixtures(config_root: Path, *names: str) -> None:
    d = transcript_dir(config_root)
    for name in names:
        shutil.copy(FIXTURES / name, d / name)


def test_01_large_corpus_streams_bounded(config_root):
    """Plan test 1: large synthetic corpus — completes; RSS bounded.

    The corpus is several times larger than the asserted allocation peak,
    so passing requires that lines are processed and dropped one at a
    time rather than the file (or its records) being materialised.
    """
    filler = "x" * 2048
    records = []
    for i in range(4096):
        records.append({
            "type": "user",
            "sessionId": f"big-{i % 4}",
            "cwd": "/home/user/proj-a",
            "timestamp": "2026-08-01T10:00:00.000Z",
            "message": {"role": "user", "content": filler},
        })
    path = transcript_dir(config_root) / "large.jsonl"
    write_records(path, records)
    corpus_bytes = path.stat().st_size
    assert corpus_bytes > 8 * 1024 * 1024

    tracemalloc.start()
    try:
        result = scan(config_root)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["files_scanned"] == 1
    assert result["lines_total"] == 4096
    assert result["sessions"] == 4
    assert peak < 2 * 1024 * 1024
    assert peak < corpus_bytes // 4


def test_02_malformed_line_mid_file(config_root):
    """Plan test 2: malformed line mid-file — counted, scan continues."""
    d = transcript_dir(config_root)
    good_before = activity("s-good", tools=[{"name": "Bash",
                                            "input": {"command": "ls"}}])
    good_after = activity("s-good2", tools=[])
    text = (json.dumps(good_before) + "\n"
            + "{{{ this is not json\n"
            + "42\n"                       # parseable, but not a record
            + json.dumps(good_after) + "\n")
    (d / "broken.jsonl").write_text(text, encoding="utf-8")

    result = scan(config_root)
    assert result["lines_malformed"] == 2
    assert result["lines_total"] == 4
    assert result["files_scanned"] == 1
    assert result["sessions"] == 2
    assert result["shell_calls"] == 1


def test_03_oversize_record_never_materialised(config_root):
    """Plan test 3: oversize single-line record — never fully materialised.

    A record whose line exceeds LIMIT is drained in bounded chunks without
    ever reaching json.loads: counted as oversize (not malformed), its
    session never appears, and peak allocation stays far below the line
    size.
    """
    payload = "A" * (48 * 1024 * 1024)
    oversize_line = json.dumps({
        "type": "assistant",
        "sessionId": "oversize-session",
        "cwd": "/home/user/proj-a",
        "timestamp": "2026-08-01T10:00:00.000Z",
        "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": "/home/user/proj-a/x", "content": payload}}]},
    })
    del payload
    ok_record = activity("ok-session", tools=[])
    path = transcript_dir(config_root) / "oversize.jsonl"
    path.write_text(oversize_line + "\n" + json.dumps(ok_record) + "\n",
                    encoding="utf-8")
    line_bytes = len(oversize_line)
    del oversize_line
    assert line_bytes > 4 * retro.LIMIT

    tracemalloc.start()
    try:
        result = scan(config_root)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["lines_oversize"] == 1
    assert result["lines_malformed"] == 0
    assert result["lines_total"] == 2
    assert "oversize-session" not in result["by_session"]
    assert "ok-session" in result["by_session"]
    # The bound is a function of the record limit, not the line size:
    # retained chunk + drain chunk + readline's internal decode buffering
    # measures ~4×LIMIT. The full line is ~50 MB.
    assert peak < 6 * retro.LIMIT
    assert peak < line_bytes // 2


def test_04_final_line_without_trailing_newline(config_root):
    """Plan test 4: final line without trailing newline — processed."""
    install_fixtures(config_root, "mixed_small.jsonl")
    raw = (FIXTURES / "mixed_small.jsonl").read_bytes()
    assert not raw.endswith(b"\n")  # the fixture's reason to exist

    result = scan(config_root)
    assert result["lines_total"] == 6
    # The final record is the Read tool_use — it must have been parsed.
    assert result["by_session"]["s-alpha"]["file_ops"] == 2


def test_05_unreadable_file(config_root):
    """Plan test 5: unreadable file — counted, scan continues."""
    if os.geteuid() == 0:
        pytest.skip("running as root: chmod 000 does not block reads")
    d = transcript_dir(config_root)
    write_records(d / "readable.jsonl", [activity("s-ok", tools=[])])
    blocked = d / "blocked.jsonl"
    write_records(blocked, [activity("s-blocked", tools=[])])
    blocked.chmod(0)
    try:
        result = scan(config_root)
    finally:
        blocked.chmod(0o644)

    assert result["files_unreadable"] == 1
    assert result["files_scanned"] == 1
    assert result["sessions"] == 1
    assert "s-blocked" not in result["by_session"]


def test_06_unknown_fields_ignored(config_root):
    """Plan test 6: unknown/new field present — ignored, no crash."""
    install_fixtures(config_root, "mixed_small.jsonl")
    result = scan(config_root)
    # The record carrying futureField/anotherNewKey is the Bash record;
    # it was processed normally.
    assert result["lines_malformed"] == 0
    assert result["shell_calls"] == 1
    assert result["sessions"] == 1


def test_07_sessions_counted_once_across_files(config_root):
    """Plan test 7: multiple sessions across files — counted once per id."""
    install_fixtures(config_root, "mixed_small.jsonl", "second_file.jsonl")
    result = scan(config_root)
    assert result["files_scanned"] == 2
    assert result["sessions"] == 2          # s-alpha spans both files
    assert result["by_session"]["s-alpha"]["tool_calls"] == 4
    assert result["by_session"]["s-beta"]["tool_calls"] == 1
    assert result["tool_calls"] == 5
    assert result["sessions_with_tools"] == 2


def test_08_zero_sessions(config_root):
    """Plan test 8: zero sessions — clear empty state."""
    # Default-root path: scan() with no argument resolves through the
    # isolated $CLAUDE_CONFIG_DIR.
    result = scan()
    assert result["files_scanned"] == 0
    assert result["sessions"] == 0
    assert result["findings"] == []
    assert result["period_start"] is None
    assert result["period_end"] is None

    # An empty transcript file is still an empty state, not an error.
    (transcript_dir(Path(os.environ["CLAUDE_CONFIG_DIR"]))
     / "empty.jsonl").write_text("", encoding="utf-8")
    result = scan()
    assert result["files_scanned"] == 1
    assert result["sessions"] == 0


def test_09_transcripts_unmodified(config_root):
    """Plan test 9: transcripts unmodified — mtime + hash unchanged."""
    install_fixtures(config_root, "mixed_small.jsonl", "second_file.jsonl")
    files = sorted((config_root / "projects").rglob("*.jsonl"))
    before = [(f, f.stat().st_mtime_ns, hashlib.sha256(f.read_bytes()).hexdigest())
              for f in files]

    scan(config_root)

    for f, mtime_ns, digest in before:
        assert f.stat().st_mtime_ns == mtime_ns
        assert hashlib.sha256(f.read_bytes()).hexdigest() == digest


def test_10_undated_activity_record_counted_in_every_window(config_root):
    """Plan test 10: undated activity record — excluded under a finite
    window, counted in records_undated, metadata records exempt.

    records_undated is a coverage characteristic, not an exclusion
    counter: the window mode decides whether the record participates in
    activity analysis, never whether its missing timestamp is counted.
    Both modes are asserted here.
    """
    d = transcript_dir(config_root)
    records = [
        activity("s-win", tools=[{"name": "Bash", "input": {"command": "ls"}}],
                 timestamp="2026-08-24T12:00:00.000Z"),
        # Undated record carrying tool activity: excluded AND counted.
        activity("s-win", tools=[{"name": "Write",
                                  "input": {"file_path": "/home/user/proj-a/f",
                                            "content": "c"}}],
                 timestamp=None),
        # Undated record with no tool activity: excluded, NOT counted.
        {"type": "user", "sessionId": "s-win", "cwd": "/home/user/proj-a",
         "message": {"role": "user", "content": "hello"}},
        # Metadata record: undated by nature, exempt from the window and
        # from records_undated; label extraction still applies.
        {"type": "custom-title", "customTitle": "Win title",
         "sessionId": "s-win"},
    ]
    write_records(d / "win.jsonl", records)

    # Finite window: the undated activity record does not participate,
    # and is counted as undated.
    result = scan(config_root, since="7d", now=FIXED_NOW)
    assert result["records_undated"] == 1
    assert result["sessions"] == 1
    assert result["by_session"]["s-win"] == {
        "tool_calls": 1, "file_ops": 0, "shell_calls": 1}
    assert result["session_labels"]["s-win"] == {
        "label": "Win title", "source": "custom-title"}

    # --since all: the same record participates in activity analysis AND
    # is still counted as undated. The undated no-tool record and the
    # undated custom-title metadata record remain uncounted either way.
    result = scan(config_root, since="all", now=FIXED_NOW)
    assert result["records_undated"] == 1
    assert result["sessions"] == 1
    assert result["by_session"]["s-win"] == {
        "tool_calls": 2, "file_ops": 1, "shell_calls": 1}
    assert result["records_excluded_by_window"] == 0
    assert result["session_labels"]["s-win"] == {
        "label": "Win title", "source": "custom-title"}


def test_11_dated_record_outside_window(config_root):
    """Plan test 11: dated record outside window — excluded; a session
    with no included record is not counted."""
    d = transcript_dir(config_root)
    write_records(d / "old.jsonl", [
        activity("s-old", tools=[{"name": "Bash", "input": {"command": "ls"}}],
                 timestamp="2026-07-01T12:00:00.000Z"),
    ])

    result = scan(config_root, since="7d", now=FIXED_NOW)
    assert result["records_excluded_by_window"] == 1
    assert result["sessions"] == 0
    assert "s-old" not in result["by_session"]
    assert "s-old" not in result["session_labels"]
    assert result["shell_calls"] == 0

    # The same corpus under --since all participates fully.
    result = scan(config_root, since="all", now=FIXED_NOW)
    assert result["sessions"] == 1
    assert result["records_excluded_by_window"] == 0


def test_12_missing_cwd_or_file_path_is_unknown(config_root):
    """Plan test 12: missing cwd / missing file_path — unknown, never a
    finding."""
    assert interpret_target(None, "/home/user/proj-a") == (None, "missing")
    assert interpret_target("", "/home/user/proj-a") == (None, "missing")
    assert interpret_target("src/x.py", None) == (None, "relative-no-cwd")

    d = transcript_dir(config_root)
    record = activity("s-x", tools=[{"name": "Write", "input": {"content": "c"}}])
    del record["cwd"]
    write_records(d / "t.jsonl", [record])
    result = scan(config_root)
    assert result["unknown_targets"] == 1
    assert result["findings"] == []


def test_13_invalid_cwd_is_unknown(config_root):
    """Plan test 13: non-absolute or invalid cwd — unknown, never a
    finding."""
    assert interpret_target("src/x.py", "rel/cwd") == (None, "relative-no-cwd")
    assert interpret_target("src/x.py", "C:\\proj") == (None, "relative-no-cwd")
    assert interpret_target("src/x.py", "$HOME/p") == (None, "relative-no-cwd")

    d = transcript_dir(config_root)
    write_records(d / "t.jsonl", [
        activity("s-x", cwd="relative/cwd",
                 tools=[{"name": "Edit",
                         "input": {"file_path": "src/x.py",
                                   "old_string": "a", "new_string": "b"}}]),
    ])
    result = scan(config_root)
    assert result["unknown_targets"] == 1
    assert result["findings"] == []


def test_14_relative_path_joined_to_own_cwd(config_root):
    """Plan test 14: relative path joined to that record's own cwd."""
    assert interpret_target("notes/a.txt", "/home/u/proj") == (
        "/home/u/proj/notes/a.txt", "ok")
    assert interpret_target("src/../lib/x.py", "/home/u/proj") == (
        "/home/u/proj/lib/x.py", "ok")
    # Each record resolves against its OWN cwd — per-record, not
    # per-session (real sessions carry multiple cwd values).
    assert interpret_target("f.txt", "/home/u/proj/sub") == (
        "/home/u/proj/sub/f.txt", "ok")

    d = transcript_dir(config_root)
    write_records(d / "t.jsonl", [
        activity("s-x", cwd="/home/user/proj-a",
                 tools=[{"name": "Write",
                         "input": {"file_path": "notes/a.txt", "content": "c"}}]),
        activity("s-x", cwd="/home/user/proj-a/sub",
                 tools=[{"name": "Write",
                         "input": {"file_path": "b.txt", "content": "c"}}]),
    ])
    result = scan(config_root)
    assert result["unknown_targets"] == 0
    assert result["file_ops"] == 2


def test_15_home_and_env_var_forms_are_unknown(config_root):
    """Plan test 15: ~/x, ~user/x, $VAR/x — unknown; never a finding."""
    assert interpret_target("~/x", "/home/u/proj") == (None, "home-relative")
    assert interpret_target("~user/x", "/home/u/proj") == (None, "home-relative")
    assert interpret_target("$VAR/x", "/home/u/proj") == (None, "env-var")

    d = transcript_dir(config_root)
    write_records(d / "t.jsonl", [
        activity("s-x", tools=[
            {"name": "Write", "input": {"file_path": "~/x", "content": "c"}},
            {"name": "Write", "input": {"file_path": "~user/x", "content": "c"}},
            {"name": "Write", "input": {"file_path": "$VAR/x", "content": "c"}},
        ]),
    ])
    result = scan(config_root)
    assert result["unknown_targets"] == 3
    assert result["unsupported_path_forms"] == 0
    assert result["findings"] == []


def test_16_windows_style_paths_are_unknown_and_counted(config_root):
    """Plan test 16: Windows-style path — unknown, counted, never a
    finding."""
    assert interpret_target("C:\\Users\\x\\f.txt", "/h") == (None, "windows")
    assert interpret_target("C:/Users/x/f.txt", "/h") == (None, "windows")
    assert interpret_target("a\\b", "/h") == (None, "windows")

    install_fixtures(config_root, "second_file.jsonl")
    result = scan(config_root)
    assert result["unknown_targets"] == 1
    assert result["unsupported_path_forms"] == 1
    assert result["findings"] == []


def test_17_bash_never_interpreted(config_root):
    """Plan test 17: Bash containing /etc/passwd — never a finding; shell
    count only."""
    install_fixtures(config_root, "mixed_small.jsonl")
    result = scan(config_root)
    assert result["shell_calls"] == 1
    assert result["findings"] == []
    assert result["unknown_targets"] == 0
    # The command body is never extracted anywhere into the aggregate.
    assert "passwd" not in json.dumps(result)


# ---- pipeline plumbing exercised by the numbered tests above ----------------


def test_since_parser_rejects_garbage():
    """--since accepts 7d/30d/all shapes only (plan §4.2, minimal parser)."""
    with pytest.raises(ValueError):
        scan(since="fortnight")
    with pytest.raises(ValueError):
        scan(since="d")
    with pytest.raises(ValueError):
        scan(since="-3d")


def test_labels_precedence_and_fallback(config_root):
    """Label precedence custom-title → slug → 8-char session id (§7.3
    storage; rendering is the renderer's concern)."""
    d = transcript_dir(config_root)
    write_records(d / "t.jsonl", [
        activity("s-titled", tools=[], slug="titled-slug"),
        {"type": "custom-title", "customTitle": "First", "sessionId": "s-titled"},
        {"type": "custom-title", "customTitle": "Renamed", "sessionId": "s-titled"},
        activity("s-slugged", tools=[], slug="slug-only"),
        activity("f41ee94f-f686-48c7-8107-8df2749c2a15", tools=[]),
    ])
    result = scan(config_root)
    labels = result["session_labels"]
    # Last observed title wins (titles can be renamed mid-session).
    assert labels["s-titled"] == {"label": "Renamed", "source": "custom-title"}
    assert labels["s-slugged"] == {"label": "slug-only", "source": "slug"}
    assert labels["f41ee94f-f686-48c7-8107-8df2749c2a15"] == {
        "label": "f41ee94f", "source": "session-id"}


def test_period_reports_observed_range(config_root):
    """period_start/period_end report the observed post-filter range."""
    d = transcript_dir(config_root)
    write_records(d / "t.jsonl", [
        activity("s-a", tools=[], timestamp="2026-08-20T00:00:00.000Z"),
        activity("s-a", tools=[], timestamp="2026-08-23T00:00:00.000Z"),
        activity("s-a", tools=[], timestamp="2026-07-01T00:00:00.000Z"),
    ])
    result = scan(config_root, since="7d", now=FIXED_NOW)
    assert result["period_start"] == "2026-08-20T00:00:00+00:00"
    assert result["period_end"] == "2026-08-23T00:00:00+00:00"
    assert result["records_excluded_by_window"] == 1


def test_declaration_activity_extraction(config_root):
    """Declaration evidence is activity, per session, prefix-matched
    (§3.2); it proves activity or an attempt, never binding."""
    d = transcript_dir(config_root)
    write_records(d / "t.jsonl", [
        activity("s-declared", tools=[
            {"name": "mcp__sentience__declare_intent", "input": {}}]),
        activity("s-plain", tools=[{"name": "Bash", "input": {"command": "ls"}}]),
    ])
    result = scan(config_root)
    assert result["sessions_with_declaration_activity"] == ["s-declared"]


# ---------------------------------------------------------------------------
# CP2 — root resolution (§5.4), suppression (§5.3), classification (§5.2/§5.5)
# ---------------------------------------------------------------------------


@pytest.fixture
def home():
    """The isolated $HOME for this test (plan §9 isolation requirement)."""
    return Path(os.environ["HOME"])


def git_repo(home: Path, name: str, *, as_file: bool = False) -> Path:
    """A directory carrying real `.git` evidence, as directory or as file."""
    repo = home / name
    repo.mkdir(parents=True, exist_ok=True)
    if as_file:
        (repo / ".git").write_text("gitdir: /elsewhere/worktrees/x\n")
    else:
        (repo / ".git").mkdir(exist_ok=True)
    return repo


def scan_records(config_root: Path, records, **kwargs) -> dict:
    write_records(transcript_dir(config_root) / "t.jsonl", records)
    return scan(config_root, **kwargs)


def write_block(path, tool: str = "Write") -> dict:
    return {"name": tool, "input": {"file_path": str(path), "content": "c"}}


def read_block(path) -> dict:
    return {"name": "Read", "input": {"file_path": str(path)}}


def classes(result) -> list:
    return sorted(f.finding_class for f in result["findings"])


def test_18_nested_cwds_in_one_repository(config_root, home):
    """Plan test 18: nested cwds in one repository — one root; no
    cross-project finding.

    This is one of the two measured false-positive classes: real sessions
    carry multiple cwd values inside one repository (6 observed in one
    session), so per-record cwd is not a project boundary.
    """
    repo = git_repo(home, "repo")
    (repo / "sub").mkdir()
    result = scan_records(config_root, [
        activity("s", cwd=str(repo), tools=[write_block(repo / "a.txt")]),
        activity("s", cwd=str(repo / "sub"),
                 tools=[write_block(repo / "sub" / "b.txt")]),
    ])
    assert result["findings"] == []
    assert result["same_project_writes"] == 2
    assert result["sessions_with_findings"] == []


def test_19_harness_worktree_strips_to_parent_repo(config_root, home):
    """Plan test 19: harness worktree path — strips to parent repo root; no
    finding.

    The second measured false-positive class: Claude Code worktrees carry
    their own `.git`, so untreated every worktree session becomes a false
    cross-project source (c14).
    """
    repo = git_repo(home, "repo")
    worktree = repo / ".claude" / "worktrees" / "abc"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: real-worktree\n")

    resolver = RootResolver(str(home))
    assert resolver.resolve(str(worktree / "deep")) == (str(repo), "worktree")

    result = scan_records(config_root, [
        activity("s", cwd=str(worktree), tools=[write_block(repo / "f.txt")]),
    ])
    assert result["findings"] == []
    assert result["same_project_writes"] == 1


def test_20_normal_git_resolution(home):
    """Plan test 20: normal git resolution (`.git` directory) — correct root."""
    repo = git_repo(home, "repo")
    (repo / "src" / "deep").mkdir(parents=True)
    resolver = RootResolver(str(home))
    assert resolver.resolve(str(repo / "src" / "deep")) == (str(repo), "git")
    assert resolver.resolve(str(repo)) == (str(repo), "git")


def test_21_git_file_does_not_establish_project_identity(home):
    """Plan test 21, as corrected by the post-CP5 erratum: a `.git` FILE is
    ambiguous identity and must never establish a distinct project.

    §5.4 originally accepted `.git` as file or directory. CP5 real-corpus
    verification showed that asserting "another project" for two
    worktrees of the SAME repository — the exact "Reader misunderstood my
    history" failure the release is meant to avoid. Reader still does not
    read `.git` contents; it degrades to UNKNOWN instead. Recall is
    deliberately sacrificed to protect the first-contact trust screen.
    """
    worktree = git_repo(home, "user-worktree", as_file=True)
    resolver = RootResolver(str(home))
    assert resolver.resolve(str(worktree)) == ("", "unknown")

    # A `.git` directory still resolves normally.
    plain = git_repo(home, "ordinary-repo")
    assert resolver.resolve(str(plain)) == (str(plain), "git")


def test_22_non_git_directory_is_unknown(home):
    """Plan test 22: non-git directory — UNKNOWN root; no fabricated root."""
    plain = home / "plain" / "nested"
    plain.mkdir(parents=True)
    resolver = RootResolver(str(home))
    assert resolver.resolve(str(plain)) == ("", "unknown")


def test_23_deleted_directory_falls_back_or_unknown(home):
    """Plan test 23: deleted/missing directory — step 2 fails → cwd-prefix
    fallback or UNKNOWN, never a fabricated root."""
    gone = home / "gone" / "sub"
    resolver = RootResolver(str(home))
    assert resolver.resolve(str(gone)) == ("", "unknown")
    assert resolver.resolve(str(gone), {str(home / "gone")}) == (
        str(home / "gone"), "cwd-prefix")


def test_24_cwd_prefix_fallback_takes_the_shortest(home):
    """Plan test 24: cwd-prefix fallback — shortest observed session cwd
    that is a component-boundary prefix."""
    resolver = RootResolver(str(home))
    assert resolver.resolve(
        "/a/proj/sub/deep", {"/a/proj/sub", "/a/proj"}) == (
        "/a/proj", "cwd-prefix")


def test_24b_sibling_directory_prefix_never_claimed(home):
    """Plan test 24b: sibling-directory prefix — `/a/proj` never claims
    `/a/proj-extras/file` (§5.4 step 3, component boundary)."""
    resolver = RootResolver(str(home))
    assert resolver.resolve("/a/proj-extras/file", {"/a/proj"}) == (
        "", "unknown")
    # Equal-at-boundary still matches, per the same rule.
    assert resolver.resolve("/a/proj", {"/a/proj"}) == ("/a/proj", "cwd-prefix")


def test_25_home_is_never_checked_or_returned(home):
    """Plan test 25: `$HOME` itself, incl. a dotfiles repo at `~/.git` —
    step 2 never checks or returns `$HOME`; home paths do not collapse into
    one project."""
    (home / ".git").mkdir()                     # a dotfiles repository
    downloads = home / "Downloads"
    downloads.mkdir()

    resolver = RootResolver(str(home))
    assert resolver.resolve(str(downloads)) == ("", "unknown")
    assert resolver.resolve(str(home)) == ("", "unknown")
    assert str(home) not in resolver.probed


def test_26_walk_never_goes_above_filesystem_root(home):
    """Plan test 26: never walks above `/` — bounded walk, plus the
    defensive depth cap."""
    resolver = RootResolver(str(home))
    assert resolver.resolve("/a/b/c") == ("", "unknown")
    assert "/" not in resolver.probed

    deep = "/" + "/".join("d%d" % i for i in range(200))
    assert resolver.resolve(deep) == ("", "unknown")
    assert len(resolver.probed) < 200            # the cap held


def test_27_symlinked_root_stays_lexical(home):
    """Plan test 27: symlinked root — documented lexical behavior; no
    `realpath` of historical paths. Ambiguity remains accepted."""
    real = git_repo(home, "real-repo")
    link = home / "link-repo"
    link.symlink_to(real)

    resolver = RootResolver(str(home))
    root, evidence = resolver.resolve(str(link))
    assert (root, evidence) == (str(link), "git")
    assert root != str(real)


def test_28_memoization_one_walk_per_directory(home):
    """Plan test 28: memoization — one `stat` walk per distinct directory."""
    repo = git_repo(home, "repo")
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)

    resolver = RootResolver(str(home))
    assert resolver.resolve(str(deep)) == (str(repo), "git")
    first = len(resolver.probed)
    assert first == 3                            # b, a, repo

    resolver.resolve(str(deep))
    assert len(resolver.probed) == first         # fully memoized

    resolver.resolve(str(repo / "a" / "c"))
    assert len(resolver.probed) == first + 1     # only the new directory


def test_29_another_repository_write_is_cross_project(config_root, home):
    """Plan test 29: another-repository write — `cross_project` finding."""
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(dest / "f.txt")]),
    ])
    (finding,) = result["findings"]
    assert finding.finding_class == "cross_project"
    assert finding.source_root == str(source)
    assert finding.dest_root == str(dest)
    assert finding.source_root_evidence == "git"
    assert finding.op_count == 1
    assert result["sessions_with_findings"] == ["s"]


def test_30_downloads_write_is_non_project(config_root, home):
    """Plan test 30: `~/Downloads` write — `non_project`, reported
    separately, never folded into the cross-project headline."""
    source = git_repo(home, "repo-a")
    downloads = home / "Downloads"
    downloads.mkdir()
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(downloads / "f.txt")]),
    ])
    (finding,) = result["findings"]
    assert finding.finding_class == "non_project"
    assert finding.source_root == str(source)
    assert finding.dest_root == ""


def test_31_claude_settings_json_is_config_finding(config_root, home):
    """Plan test 31: `~/.claude/settings.json` write — `claude_config`."""
    source = git_repo(home, "repo-a")
    result = scan_records(config_root, [
        activity("s", cwd=str(source),
                 tools=[write_block(config_root / "settings.json")]),
    ])
    (finding,) = result["findings"]
    assert finding.finding_class == "claude_config"
    assert finding.dest_root == ""
    assert result["suppressed"] == {}


def test_32_claude_json_is_config_finding(config_root, home):
    """Plan test 32: `~/.claude.json` write — `claude_config` finding."""
    source = git_repo(home, "repo-a")
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(home / ".claude.json")]),
    ])
    (finding,) = result["findings"]
    assert finding.finding_class == "claude_config"


def test_33_control_surfaces_are_reportable(config_root, home):
    """Plan test 33: `~/.claude/{hooks,skills,commands,agents}/**` writes —
    `claude_config` findings, reportable, not suppressed."""
    source = git_repo(home, "repo-a")
    targets = [config_root / d / "x.md"
               for d in ("hooks", "skills", "commands", "agents")]
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(t) for t in targets]),
    ])
    assert classes(result) == ["claude_config"] * 4
    assert result["suppressed"] == {}


def test_33b_unknown_claude_path_falls_through(config_root, home):
    """Plan test 33b: unknown non-suppressed `~/.claude/` path — NOT
    `claude_config`; falls through to ordinary classification, never rank-1.

    Classifying by positive list rather than "everything not suppressed" is
    what stops a new Claude Code auto-directory from reintroducing
    infrastructure noise as the strongest finding class.
    """
    source = git_repo(home, "repo-a")
    target = config_root / "todos" / "x.json"
    assert not is_claude_config(str(target), str(config_root), str(home))

    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(target)]),
    ])
    (finding,) = result["findings"]
    assert finding.finding_class == "non_project"


@pytest.mark.parametrize("rule,relative", [
    ("S1", "projects/proj-x/memory/notes.md"),
    ("S2", "projects/proj-x/session/tool-results/r.json"),
    ("S3", "projects/proj-x/session.jsonl"),
    ("S4", "plans/scratch.md"),
])
def test_34_to_37_config_root_suppression_rules(config_root, home, rule, relative):
    """Plan tests 34–37: suppression rules S1–S4 individually — suppressed,
    counted under their own name, absent from findings and secondary
    counts."""
    source = git_repo(home, "repo-a")
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(config_root / relative)]),
    ])
    assert result["suppressed"] == {rule: 1}
    assert result["findings"] == []
    assert result["outside_cwd_secondary"] == 0
    assert result["unknown_root_writes"] == 0
    assert result["same_project_writes"] == 0


def test_38_harness_scratchpad_suppression(config_root, home):
    """Plan test 38: suppression rule S5 — `/tmp/claude-*/**` and
    `/private/tmp/claude-*/**`."""
    source = git_repo(home, "repo-a")
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[
            write_block("/tmp/claude-501/x/scratch.txt"),
            write_block("/private/tmp/claude-501/y/scratch.txt"),
        ]),
    ])
    assert result["suppressed"] == {"S5": 2}
    assert result["findings"] == []
    assert result["outside_cwd_secondary"] == 0


def test_39_other_projects_claude_config_is_cross_project(config_root, home):
    """Plan test 39: another project's `.claude/settings.local.json` —
    `cross_project` by root logic, not the config anchor."""
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    target = dest / ".claude" / "settings.local.json"
    target.parent.mkdir(parents=True)
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(target)]),
    ])
    (finding,) = result["findings"]
    assert finding.finding_class == "cross_project"
    assert finding.dest_root == str(dest)


def test_40_source_root_unknown_suppresses_the_claim(config_root, home):
    """Plan test 40: source root UNKNOWN — cross-project claim suppressed,
    `unknown_root_writes` counted.

    The UNKNOWN-side rule is asymmetric by design: without knowing where
    the session worked, "another project" cannot be asserted. The
    destination-side-only case stays a `non_project` finding (test 30).
    """
    dest = git_repo(home, "repo-b")
    record = activity("s", tools=[write_block(dest / "f.txt")])
    del record["cwd"]                    # an absolute target stays classifiable
    result = scan_records(config_root, [record])

    assert result["findings"] == []
    assert result["unknown_root_writes"] == 1
    assert result["unknown_targets"] == 0


def test_41_reads_are_never_findings(config_root, home):
    """Plan test 41: reads outside the project — never findings.

    §5.2 as corrected by critic finding 8: reads never enter the secondary
    count either; they contribute only to `file_ops`.
    """
    source = git_repo(home, "repo-a")
    other = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[read_block(other / "f.txt")]),
    ])
    assert result["findings"] == []
    assert result["file_ops"] == 1
    assert result["outside_cwd_secondary"] == 0


def test_42_repeated_writes_aggregate_into_one_finding(config_root, home):
    """Plan test 42: repeated writes to one target in one session — one
    finding, `op_count` aggregated, never N display rows."""
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[
            write_block(dest / "f.txt"),
            write_block(dest / "f.txt", tool="Edit"),
            write_block(dest / "f.txt"),
        ]),
    ])
    (finding,) = result["findings"]
    assert finding.op_count == 3
    assert finding.tool == "Write"       # last tool observed for the target


def test_43_same_target_two_sessions_two_findings(config_root, home):
    """Plan test 43: same target, two sessions — two findings (the
    cardinality rule is session + class + target)."""
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        activity("s1", cwd=str(source), tools=[write_block(dest / "f.txt")]),
        activity("s2", cwd=str(source), tools=[write_block(dest / "f.txt")]),
    ])
    assert len(result["findings"]) == 2
    assert sorted(f.session_id for f in result["findings"]) == ["s1", "s2"]
    assert result["sessions_with_findings"] == ["s1", "s2"]


def test_suppression_and_config_lists_are_narrow(config_root, home):
    """The named lists are narrow by design: a future auto-directory must
    fail visibly (an unexplained finding), never silently (§5.3)."""
    root, hm = str(config_root), str(home)
    # Not suppressed: configuration is deliberately kept reportable.
    assert suppression_rule(root + "/settings.json", root, hm) is None
    assert suppression_rule(root + "/hooks/h.sh", root, hm) is None
    # A new auto-directory is neither suppressed nor rank-1.
    assert suppression_rule(root + "/todos/x.json", root, hm) is None
    assert not is_claude_config(root + "/todos/x.json", root, hm)
    # S5 needs the claude- prefix.
    assert suppression_rule("/tmp/other/x", root, hm) is None
    assert suppression_rule("/tmp/claude-1/x", root, hm) == "S5"


# ---------------------------------------------------------------------------
# CP3 — labels and language (§7), three report states, ranking and caps (§8)
# ---------------------------------------------------------------------------

def flat(rendered: str) -> str:
    """Whitespace-normalized render, for text the screen wraps."""
    return " ".join(rendered.split())


FORBIDDEN_WORDS = (
    "violation", "drift", "unauthorized", "out of scope", "successfully wrote",
)


def cross_project_corpus(config_root, home, sessions):
    """Build a corpus where each session writes into its own set of repos.

    ``sessions`` maps session id -> list of (repo name, op count).
    """
    source = git_repo(home, "repo-src")
    records = []
    for session_id, destinations in sessions.items():
        for name, ops in destinations:
            dest = git_repo(home, name)
            records.append(activity(
                session_id, cwd=str(source),
                tools=[write_block(dest / "f.txt")] * ops,
            ))
    return scan_records(config_root, records)


def test_44_custom_title_label_survives_finite_window(config_root, home):
    """Plan test 44: `custom-title` present (records are undated) — label =
    customTitle, last observed wins, set even under a finite window."""
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        {"type": "custom-title", "customTitle": "First", "sessionId": "s"},
        {"type": "custom-title", "customTitle": "Alpha work", "sessionId": "s"},
        activity("s", cwd=str(source), tools=[write_block(dest / "f.txt")],
                 timestamp="2026-08-24T12:00:00.000Z"),
    ], since="7d", now=FIXED_NOW)
    assert result["session_labels"]["s"]["source"] == "custom-title"
    assert '"Alpha work"' in render_scan(result)


def test_45_slug_label(config_root, home):
    """Plan test 45: no title, `slug` present — label = slug."""
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        activity("s", cwd=str(source), slug="recover-archived-work",
                 tools=[write_block(dest / "f.txt")]),
    ])
    assert result["session_labels"]["s"]["source"] == "slug"
    assert '"recover-archived-work"' in render_scan(result)


def test_46_session_id_fallback_looks_deliberate(config_root, home):
    """Plan test 46: neither — shortened session ID, rendered deliberately.

    The fallback renders bare rather than quoted, so it reads as an
    identifier and never as a title the user chose (§7.3).
    """
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        activity("489aab2f-0c52-41d0-9304-fbd6f9040ab7", cwd=str(source),
                 tools=[write_block(dest / "f.txt")]),
    ])
    rendered = render_scan(result)
    assert "489aab2f" in rendered
    assert '"489aab2f' not in rendered
    assert "489aab2f-0c52" not in rendered


def test_47_prompt_text_appears_nowhere(config_root, home):
    """Plan test 47: `last-prompt` records — `lastPrompt` text appears
    NOWHERE in output or aggregate.

    Reader's trust proposition is that it does not use prompt content; a
    tool cannot say so while displaying a prompt as a label.
    """
    secret = "PROMPTSECRETdeletetheproductiondatabase"
    source = git_repo(home, "repo-a")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        {"type": "last-prompt", "sessionId": "s", "lastPrompt": secret,
         "timestamp": "2026-08-24T12:00:00.000Z"},
        activity("s", cwd=str(source), tools=[write_block(dest / "f.txt")],
                 lastPrompt=secret),
    ])
    assert secret not in json.dumps(retro.json_payload(result))
    assert secret not in render_scan(result)


def test_48_declaration_activity_is_never_an_objective(config_root, home):
    """Plan test 48: declaration activity in 1 of 3 sessions — "activity in
    1 of 3"; never "had a runtime objective"."""
    source = git_repo(home, "repo-a")
    result = scan_records(config_root, [
        activity("s1", cwd=str(source), tools=[
            {"name": "mcp__sentience__declare_intent", "input": {}}]),
        activity("s2", cwd=str(source), tools=[]),
        activity("s3", cwd=str(source), tools=[]),
    ])
    rendered = render_scan(result)
    assert "activity in 1 of 3" in flat(rendered)
    assert "runtime objective" not in rendered


@pytest.mark.parametrize("state", ["zero", "one", "many"])
def test_49_forbidden_vocabulary_absent_from_every_state(config_root, home, state):
    """Plan test 49: report text sweep — violation / drift / unauthorized /
    out of scope / successfully wrote absent from all three states."""
    if state == "zero":
        result = scan_records(config_root, [
            activity("s", cwd=str(git_repo(home, "repo-a")), tools=[])])
    elif state == "one":
        result = cross_project_corpus(config_root, home, {"s": [("repo-b", 2)]})
    else:
        result = cross_project_corpus(
            config_root, home, {"s1": [("repo-b", 2)], "s2": [("repo-c", 1)]})
    rendered = render_scan(result).lower()
    for word in FORBIDDEN_WORDS:
        assert word not in rendered


def test_50_write_qualifier_present_when_findings_shown(config_root, home):
    """Plan test 50: write-qualifier line — present in any state showing
    write findings (attempt-grade language, §7.2)."""
    qualifier = ("Counts are write operations Claude issued as recorded in\n"
                 "its history. Reader does not verify completion.")
    one = cross_project_corpus(config_root, home, {"s": [("repo-b", 2)]})
    assert qualifier in render_scan(one)

    many = cross_project_corpus(
        config_root, home, {"s1": [("repo-b", 2)], "s2": [("repo-c", 1)]})
    assert qualifier in render_scan(many)


def test_51_state_zero_leads_with_what_was_found(config_root, home):
    """Plan test 51: no sessions stand out — leads with "no reportable …
    was found in the activity Reader could classify", never confinement
    language, no "0 findings"."""
    result = scan_records(config_root, [
        activity("s", cwd=str(git_repo(home, "repo-a")), tools=[
            {"name": "Bash", "input": {"command": "ls"}}]),
    ])
    rendered = render_scan(result)
    assert result["sessions_with_findings"] == []
    assert ("No reportable project-boundary write activity was found\n"
            "in the activity Reader could classify.") in rendered
    for banned in ("0 findings", "nothing to report", "stayed inside",
                   "clean", "no issues"):
        assert banned not in rendered
    # A zero screen shows no write findings, so it carries no qualifier.
    assert "Reader does not verify completion" not in rendered


@pytest.mark.parametrize("state", ["zero", "one", "many"])
def test_51b_fixed_elements_present_and_ordered(config_root, home, state):
    """Plan test 51b: all three states — fixed elements present and ordered
    per §8.2, with the canonical reviewer statement on every screen."""
    if state == "zero":
        result = scan_records(config_root, [
            activity("s", cwd=str(git_repo(home, "repo-a")), tools=[])])
    elif state == "one":
        result = cross_project_corpus(config_root, home, {"s": [("repo-b", 2)]})
    else:
        result = cross_project_corpus(
            config_root, home, {"s1": [("repo-b", 2)], "s2": [("repo-c", 1)]})
    rendered = render_scan(result)

    reviewer = ("Reader is a retrospective reviewer, not live governance.\n"
                "It cannot establish what you intended or authorized, or\n"
                "whether an action complied with policy.")
    bridge = ("This review is an example of what retrospective history\n"
              "can reveal. Sentience Governor goes further by evaluating\n"
              "declared objective, scope, execution and policy while the\n"
              "agent is running.")
    footer = "  sentience init claude-code        govern the next session"

    assert rendered.startswith("Sentience · Retrospective Review\n")
    assert "· local · transcripts read-only" in rendered
    assert reviewer in rendered
    assert "Not evaluated" in rendered
    assert "Reader does not inspect or use your prompt content." in rendered
    assert bridge in rendered
    assert rendered.rstrip("\n").endswith(footer)

    # Ordering: reviewer statement → Not evaluated → bridge → footer, and
    # the limitations never sit inside the aha paragraph.
    assert (rendered.index(reviewer)
            < rendered.index("Not evaluated")
            < rendered.index(bridge)
            < rendered.index(footer))
    # The canonical statement appears exactly once — no shorthand substitute
    # and no duplication.
    assert rendered.count(reviewer) == 1


def test_52_state_one_is_the_hero_screen(config_root, home):
    """Plan test 52: exactly one session stands out (containing several
    findings) — hero render selected by session count; surprising fact
    first, then evidence, then limitations."""
    source = git_repo(home, "repo-src")
    dest = git_repo(home, "repo-b")
    downloads = home / "Downloads"
    downloads.mkdir()
    result = scan_records(config_root, [
        {"type": "custom-title", "customTitle": "Alpha work", "sessionId": "s"},
        activity("s", cwd=str(source), tools=(
            [write_block(dest / ("f%d.txt" % i)) for i in range(3)]
            + [write_block(downloads / "z.txt")]
            + [{"name": "Bash", "input": {"command": "ls"}}])),
    ])
    rendered = render_scan(result)
    assert len(result["sessions_with_findings"]) == 1
    assert len(result["findings"]) == 4      # several findings, one session

    assert "One session stands out." in rendered
    assert '"Alpha work"' in rendered
    assert "working in  ~/repo-src" in rendered
    assert "Claude targeted writes into another project:" in rendered
    # Resolver-honest wording, never "outside any project", and reported
    # as operation volume rather than an inferred count of locations.
    assert ("and 1 write operations targeted locations where Reader"
            in flat(rendered))
    assert "Showing representative destinations." in rendered
    assert "outside any project" not in rendered
    # Hierarchy: the fact and its evidence precede the limitations.
    assert (rendered.index("One session stands out.")
            < rendered.index("~/repo-b")
            < rendered.index("Not evaluated"))
    assert "1 shell commands in this session" in rendered


def test_53_state_n_rows_and_session_ordering(config_root, home):
    """Plan test 53: multiple sessions stand out — numbered rows with label
    + working-in + one-line summary, ordered by the §8.4 session ranking;
    canonical reviewer statement under the list caption."""
    result = cross_project_corpus(config_root, home, {
        "s-small": [("repo-x", 1)],
        "s-big": [("repo-y", 5), ("repo-z", 4)],
    })
    rendered = render_scan(result)
    assert result["sessions_with_findings"] == ["s-big", "s-small"]
    assert "2 sessions stand out." in rendered
    assert "  1. s-big" in rendered
    assert "  2. s-small" in rendered
    assert "     targeted writes into 2 other projects" in rendered
    assert "     targeted writes into 1 other project" in rendered
    caption = "Showing the strongest retrospective findings first."
    reviewer = "Reader is a retrospective reviewer, not live governance."
    assert rendered.index(caption) < rendered.index(reviewer)
    # No scores, severities or risk labels anywhere.
    for banned in ("severity", "risk", "score", "confidence"):
        assert banned not in rendered.lower()


def test_54_ranking_precedence_by_class(config_root, home):
    """Plan test 54: ranking precedence — claude_config sorts above
    cross_project above non_project."""
    source = git_repo(home, "repo-src")
    dest = git_repo(home, "repo-b")
    downloads = home / "Downloads"
    downloads.mkdir()
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[
            write_block(downloads / "z.txt"),
            write_block(dest / "f.txt"),
            write_block(config_root / "settings.json"),
        ]),
    ])
    assert [f.finding_class for f in result["findings"]] == [
        "claude_config", "cross_project", "non_project"]
    rendered = render_scan(result)
    assert (rendered.index("global configuration")
            < rendered.index("~/repo-b")
            < rendered.index("~/Downloads"))


def test_55_deterministic_tie_break(config_root, home):
    """Plan test 55: deterministic tie-break — equal counts give stable
    label/target ordering across runs, for finding and session order."""
    sessions = {"s-b": [("repo-q", 1)], "s-a": [("repo-p", 1)]}
    first = cross_project_corpus(config_root, home, sessions)
    order = [(f.session_id, f.target) for f in first["findings"]]
    sessions_order = first["sessions_with_findings"]
    assert sessions_order == ["s-a", "s-b"]      # label ascending

    for _ in range(3):
        shutil.rmtree(config_root / "projects")
        (config_root / "projects").mkdir()
        again = cross_project_corpus(config_root, home, sessions)
        assert [(f.session_id, f.target) for f in again["findings"]] == order
        assert again["sessions_with_findings"] == sessions_order


def test_56_display_cap_ranks_before_truncating(config_root, home, monkeypatch):
    """Plan test 56: display cap exceeded — ranking computed pre-truncation;
    omitted counts stated. Display truncation can never masquerade as
    "strongest findings"."""
    monkeypatch.setattr(retro, "MAX_DISPLAY_FINDINGS", 3)
    source = git_repo(home, "repo-src")
    dest = git_repo(home, "repo-b")
    downloads = home / "Downloads"
    downloads.mkdir()
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=(
            [write_block(downloads / ("z%d.txt" % i)) for i in range(4)]
            + [write_block(dest / "f.txt")]
            + [write_block(config_root / "settings.json")])),
    ])
    assert len(result["findings"]) == 3
    assert result["findings_omitted"] == 3
    # The survivors are the strongest by class, not the first collected.
    assert result["findings"][0].finding_class == "claude_config"
    assert result["findings"][1].finding_class == "cross_project"
    assert "3 findings not shown" in flat(render_scan(result))


def test_57_collection_bound_overflow_is_reported(config_root, home, monkeypatch):
    """Plan test 57: `MAX_TARGETS` overflow — `targets_omitted` set and
    reported, with the honest qualifier that collection itself was bounded."""
    monkeypatch.setattr(retro, "MAX_TARGETS", 2)
    source = git_repo(home, "repo-src")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        activity("s", cwd=str(source),
                 tools=[write_block(dest / ("f%d.txt" % i)) for i in range(5)]),
    ])
    assert result["targets_omitted"] == 3
    assert len(result["findings"]) == 2
    assert ("3 targets beyond the collection bound were not ranked"
            in flat(render_scan(result)))


def test_58_json_serializes_the_aggregate(config_root, home):
    """Plan test 58: `--json` — serializes the full §3.2 aggregate, with
    sets and tuples as sorted lists and objects."""
    result = cross_project_corpus(config_root, home, {"s": [("repo-b", 2)]})
    payload = retro.json_payload(result)
    round_tripped = json.loads(json.dumps(payload))

    for field in ("files_scanned", "files_unreadable", "lines_total",
                  "lines_malformed", "lines_oversize", "records_undated",
                  "records_excluded_by_window", "sessions",
                  "sessions_with_tools", "sessions_with_declaration_activity",
                  "period_start", "period_end", "tool_calls", "file_ops",
                  "shell_calls", "by_session", "unknown_targets",
                  "unsupported_path_forms", "session_labels", "suppressed",
                  "unknown_root_writes", "outside_cwd_secondary",
                  "same_project_writes", "sessions_with_findings", "findings",
                  "findings_omitted", "targets_omitted", "scan_seconds",
                  "since"):
        assert field in round_tripped, field

    (finding,) = round_tripped["findings"]
    assert finding["finding_class"] == "cross_project"
    assert finding["op_count"] == 2          # counts recorded operations
    assert set(finding) == set(retro.Finding._fields)

    # The public contract is exactly §3.2: private renderer metadata (the
    # `_home` prefix render_scan needs to abbreviate paths) must never leak
    # into the JSON surface, and no key may be underscore-prefixed.
    assert set(round_tripped) == set(retro.PUBLIC_FIELDS)
    assert "_home" in result and "_home" not in round_tripped
    assert "home" not in round_tripped
    assert not [key for key in round_tripped if key.startswith("_")]


def test_state_zero_shows_coverage_limits_when_degraded(config_root, home):
    """Coverage limits render inside `Not evaluated`, never in the aha
    paragraph (§8.1/§8.2)."""
    d = transcript_dir(config_root)
    write_records(d / "ok.jsonl", [
        activity("s", cwd=str(git_repo(home, "repo-a")), tools=[])])
    (d / "broken.jsonl").write_text("{{{ not json\n", encoding="utf-8")

    result = scan(config_root)
    rendered = render_scan(result)
    assert "1 malformed records skipped" in flat(rendered)
    assert rendered.index("Not evaluated") < rendered.index("malformed")


def test_screen_skeleton_order_is_fixed(config_root, home):
    """The locked screen skeleton (§8.2): header → sessions/range context
    line → state content → reviewer statement → Not evaluated → bridge →
    footer.

    A rank-1 `claude_config` finding is "shown first" as the first element
    of the state's main content — it never displaces the context line from
    directly beneath the header.
    """
    source = git_repo(home, "repo-src")
    dest = git_repo(home, "repo-b")
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[
            write_block(config_root / "settings.json"),
            write_block(dest / "f.txt"),
        ]),
    ])
    rendered = render_scan(result)
    lines = rendered.split("\n")

    assert lines[0] == "Sentience · Retrospective Review"
    assert lines[1] == ""
    assert lines[2] == "1 Claude Code session reviewed"
    assert lines[3].endswith("· local · transcripts read-only")

    assert (rendered.index("· local · transcripts read-only")
            < rendered.index("global configuration")
            < rendered.index("One session stands out.")
            < rendered.index("Reader is a retrospective reviewer")
            < rendered.index("Not evaluated")
            < rendered.index("This review is an example")
            < rendered.index("sentience init claude-code"))


# ---------------------------------------------------------------------------
# CP4 — CLI wiring, the two shared-system bypasses (§10.2), and the skill
# ---------------------------------------------------------------------------

DEAD_HOOK = "/nonexistent/bin/sentience-claude-code-hook"


def managed_hook_doc() -> dict:
    """A settings document carrying MANAGED Sentience evidence."""
    from sentience_governor.cli import hook_config as hc
    entry = {"matcher": "", "hooks": [{"type": "command", "command": DEAD_HOOK}]}
    return {"hooks": {event: [dict(entry)] for event in hc.GOVERNED_EVENTS}}


@pytest.fixture
def evidence_project(tmp_path, monkeypatch):
    """A project outside any git repo that carries Sentience evidence, with a
    live hook binary available — so the seam has real repair work to do."""
    from sentience_governor.cli import ux
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    local = project / ".claude" / "settings.local.json"
    local.write_text(json.dumps(managed_hook_doc(), indent=2) + "\n")

    live = tmp_path / "install" / "bin" / "sentience-claude-code-hook"
    live.parent.mkdir(parents=True)
    live.write_text("#!/bin/sh\nexit 0\n")
    live.chmod(0o755)
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: str(live))
    monkeypatch.chdir(project)
    return local


def file_state(path: Path):
    return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()


def run_cli(monkeypatch, *argv):
    from sentience_governor.cli import ux
    monkeypatch.setattr(sys, "argv", ["sentience", *argv])
    return ux.main()


def test_59_scan_on_a_fresh_install(config_root, home, monkeypatch, capsys):
    """Plan test 59: `scan` on a fresh install — no email prompt, no
    outbound request, result renders (§10.2).

    The first-run bypass is new machinery in this release: `main()`
    previously called the flow unconditionally.
    """
    from sentience_governor.cli import ux

    first_run_calls = []
    monkeypatch.setattr(ux, "maybe_run_first_run_flow",
                        lambda **kwargs: first_run_calls.append("called"))

    def no_network(*args, **kwargs):
        raise AssertionError("scan opened a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    transcript_dir(config_root)
    assert run_cli(monkeypatch, "scan") == 0

    out = capsys.readouterr().out
    assert "Sentience · Retrospective Review" in out
    assert "sentience init claude-code" in out
    assert first_run_calls == []
    # Nothing was created under the fresh HOME beyond what the test made.
    assert not (Path(home) / ".sentience").exists()


def test_59b_other_commands_keep_their_first_run_behaviour(monkeypatch):
    """The bypass is narrow: every other dispatched command still runs the
    first-run flow, and a bare `sentience` still does too."""
    from sentience_governor.cli import ux

    assert ux._bypasses_first_run(ux.run_scan) is True
    assert ux._bypasses_first_run(ux.run_pulse) is False
    assert ux._bypasses_first_run(ux.run_init_claude_code) is False
    assert ux._bypasses_first_run(None) is False


def test_60_scan_runs_no_seam_convergence(config_root, evidence_project,
                                          monkeypatch, capsys):
    """Plan test 60: `scan` in a project with Sentience evidence —
    `.claude/settings.local.json` untouched; the seam did not run."""
    from sentience_governor.cli import ux
    monkeypatch.setattr(ux, "maybe_run_first_run_flow", lambda **kwargs: None)

    before = file_state(evidence_project)
    transcript_dir(config_root)
    assert run_cli(monkeypatch, "scan") == 0
    capsys.readouterr()

    assert file_state(evidence_project) == before


def test_60b_other_commands_still_converge(evidence_project, monkeypatch,
                                           capsys):
    """The seam still repairs for other commands — the bypass costs exactly
    one opportunistic convergence, on `scan` only.

    This is also what makes test 60 meaningful: the same project, the same
    dead entry, and a command that is not `scan` does write.
    """
    from sentience_governor.cli import ux
    from sentience_governor.cli.hook_config import run_seam_convergence

    assert ux._bypasses_seam(ux.run_scan) is True
    assert ux._bypasses_seam(ux.run_init_claude_code) is True
    assert ux._bypasses_seam(ux.run_pulse) is False

    before = file_state(evidence_project)
    run_seam_convergence()
    capsys.readouterr()
    assert file_state(evidence_project) != before
    assert DEAD_HOOK not in evidence_project.read_text()


def test_61_mutation_guard_on_the_seam_bypass(config_root, evidence_project,
                                              monkeypatch, capsys):
    """Plan test 61: mutation guard — disable the seam bypass so the seam
    runs for `run_scan`; test 60's property is then violated, and restoring
    the predicate makes it hold again.

    In-process, per the `tests/v0_3_0_3/test_mutations.py` pattern:
    plant → fail → restore → pass. The mutation is scoped to a
    monkeypatch context so restoring it does not also undo the fixture's
    chdir and binary resolution.
    """
    from sentience_governor.cli import ux
    monkeypatch.setattr(ux, "maybe_run_first_run_flow", lambda **kwargs: None)

    before = file_state(evidence_project)

    with monkeypatch.context() as mutation:
        mutation.setattr(ux, "_bypasses_seam",
                         lambda func: func is ux.run_init_claude_code)
        transcript_dir(config_root)
        assert run_cli(monkeypatch, "scan") == 0
        capsys.readouterr()
        # Test 60's property is violated under the mutation.
        assert file_state(evidence_project) != before

    # Restored: rewrite the evidence and prove the property holds again.
    evidence_project.write_text(json.dumps(managed_hook_doc(), indent=2) + "\n")
    restored = file_state(evidence_project)
    assert run_cli(monkeypatch, "scan") == 0
    capsys.readouterr()
    assert file_state(evidence_project) == restored


def test_cli_since_and_json_surface(config_root, home, monkeypatch, capsys):
    """`--since` defaults to `all` and `--json` emits exactly the locked
    public payload — no private renderer metadata on the CLI surface."""
    from sentience_governor.cli import ux
    monkeypatch.setattr(ux, "maybe_run_first_run_flow", lambda **kwargs: None)

    source = git_repo(Path(home), "repo-a")
    dest = git_repo(Path(home), "repo-b")
    scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(dest / "f.txt")]),
    ])

    assert run_cli(monkeypatch, "scan", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == set(retro.PUBLIC_FIELDS)
    assert "_home" not in payload
    assert payload["since"] == "all"
    assert payload["findings"][0]["finding_class"] == "cross_project"

    assert run_cli(monkeypatch, "scan", "--since", "7d", "--json") == 0
    assert json.loads(capsys.readouterr().out)["since"] == "7d"

    with pytest.raises(SystemExit):
        run_cli(monkeypatch, "scan", "--since", "fortnight")


def test_sentience_review_skill_is_a_thin_wrapper():
    """Plan §8.5: the skill is a thin `!`-wrapper over `sentience scan`
    with a verbatim-render instruction — no second analysis engine, no
    objective inference, no reinterpretation, no verdicts."""
    from sentience_governor.cli.ux import _bundled_skills

    bundled = _bundled_skills()
    assert "sentience-review" in bundled
    body = bundled["sentience-review"]

    assert "disable-model-invocation: true" in body
    assert "allowed-tools: Bash(sentience scan *)" in body
    assert "!`sentience scan`" in body
    assert "Render the command output above verbatim in a code block." in body
    assert "Interpretation (not Sentience output):" in body
    # The wrapper forbids inventing analysis rather than performing any.
    assert "Do not" in body
    assert "summarize, reinterpret, reformat" in body
    assert "severity" in body and "risk or confidence language" in body
    assert "compute substitute results" in body


def test_sentience_review_skill_installs_with_the_others(tmp_path):
    """The existing installer picks the new skill up with no mechanism
    change: it reaches existing users on their next `init claude-code`."""
    from sentience_governor.cli.ux import _install_skills

    skills_root = tmp_path / ".claude" / "skills"
    _install_skills(skills_root, force=False)
    installed = skills_root / "sentience-review" / "SKILL.md"
    assert installed.is_file()
    assert "!`sentience scan`" in installed.read_text()


# ---------------------------------------------------------------------------
# CP6 — regressions for the two CP5 real-corpus discoveries
# ---------------------------------------------------------------------------


def test_cp5_same_repo_worktree_is_never_another_project(config_root, home):
    """CP5 discovery 1: a user-created worktree of the SAME repository must
    not produce a cross-project claim.

    On the real corpus this rendered `~/sentience-governor` →
    `~/sentience-governor-v031` as "another project" when both are one
    repository. Reader does not read `.git` contents to tell them apart —
    it declines to claim a project at all.
    """
    source = git_repo(home, "repo")            # .git directory
    worktree = git_repo(home, "repo-wt", as_file=True)   # .git file
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[write_block(worktree / "f.txt")]),
    ])

    assert [f.finding_class for f in result["findings"]] == ["non_project"]
    assert result["findings"][0].dest_root == ""
    rendered = render_scan(result)
    assert "another project" not in rendered
    assert "Reader cannot identify a project today" in flat(rendered)


def test_cp5_non_project_reports_volume_not_location_count(config_root, home):
    """CP5 discovery 2: descendants of one tree must not be presented as
    many distinct "locations".

    Reader does not know how many locations there were; it knows the
    operation volume and can show representative destinations.
    """
    source = git_repo(home, "repo-src")
    tree = home / "deleted-tree"
    for sub in ("source/.github/workflows", "source/pkg/profile", "evidence"):
        (tree / sub).mkdir(parents=True)
    result = scan_records(config_root, [
        activity("s", cwd=str(source), tools=[
            write_block(tree / "source/.github/workflows/ci.yml"),
            write_block(tree / "source/pkg/profile/p.py"),
            write_block(tree / "evidence/e.txt"),
            write_block(tree / "evidence/e.txt"),
        ]),
    ])
    rendered = render_scan(result)

    # Three findings, one tree, four recorded operations.
    assert len(result["findings"]) == 3
    assert ("Claude targeted 4 write operations into locations where"
            in flat(rendered))
    assert "~/deleted-tree/…" in rendered
    assert "Showing representative destinations." in rendered
    # The old, noisy formulation must not return.
    assert "3 other locations" not in rendered
    assert "other location" not in rendered
