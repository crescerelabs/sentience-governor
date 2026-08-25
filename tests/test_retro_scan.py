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
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentience_governor import retro
from sentience_governor.retro import interpret_target, scan

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
