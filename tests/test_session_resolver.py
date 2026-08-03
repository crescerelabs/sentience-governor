"""F-V4 + F-V10: stable session ordering and a shared open/analyze resolver.

F-V4: `--latest` previously ordered by file mtime. A live session being
appended (the operator's own session) keeps bumping its mtime, so
`sentience list` and `sentience analyze --latest` could disagree between
consecutive invocations. Fix: order by the stable session-start time
(first event's timestamp_utc), with mtime only as a tiebreaker.

F-V10: `sentience open` previously accepted only session-id prefixes,
while `analyze` also accepted file paths. Both now share one resolver.
"""

import argparse
import json
import os
from pathlib import Path

import pytest

from sentience_governor.cli import ux
from sentience_governor.cli.ux import (
    _list_session_files,
    _resolve_session_target,
    run_open,
)


def _event(seq: int, sid: str, ts_iso: str) -> dict:
    return {
        "event_id": f"evt-{seq}",
        "event_type": "AGENT_REGISTERED" if seq == 1 else "INTENT_DECLARED",
        "session_id": sid,
        "event_sequence_number": seq,
        "previous_event_id": None,
        "timestamp_utc": ts_iso,
        "primitive": "REGISTRATION",
        "payload": {},
        "advisory_flags": [],
        "policy_violations": [],
    }


def _write_session(trace_dir: Path, sid: str, start_iso: str, mtime: float = None) -> Path:
    f = trace_dir / f"{sid}.jsonl"
    events = [_event(1, sid, start_iso), _event(2, sid, start_iso)]
    f.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def test_latest_uses_start_time_not_mtime(tmp_path):
    """The drift scenario: an earlier-started session that was appended
    most recently (newest mtime) must NOT outrank a later-started one."""
    trace_dir = tmp_path / "tr"
    trace_dir.mkdir()
    # 'live' started earlier (10:00) but was just appended → newest mtime.
    _write_session(trace_dir, "live-older-start", "2026-05-21T10:00:00.000Z",
                   mtime=10_000_000)
    # 'recent' started later (10:05) but mtime is older.
    _write_session(trace_dir, "recent-newer-start", "2026-05-21T10:05:00.000Z",
                   mtime=9_000_000)

    ordered = _list_session_files(trace_dir)
    # Later START time wins, despite older mtime.
    assert ordered[0].stem == "recent-newer-start"


def test_list_open_analyze_agree_on_latest(tmp_path, monkeypatch):
    """All three (list ordering, open --latest, analyze --latest) resolve
    the same canonical 'latest' session — despite mtime drift."""
    trace_dir = tmp_path / "tr"
    trace_dir.mkdir()
    # alpha started earlier but has the newest mtime (drift); beta started
    # later. The operator means beta by "latest".
    _write_session(trace_dir, "alpha", "2026-05-21T09:00:00.000Z", mtime=10_000_000)
    _write_session(trace_dir, "beta", "2026-05-21T09:30:00.000Z", mtime=9_000_000)
    monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(trace_dir))

    # `list` ordering (row #1)
    list_row1 = _list_session_files(trace_dir)[0].stem
    # `open --latest` and `analyze --latest` both go through the resolver
    # with target=None, latest=True.
    open_target, _ = _resolve_session_target(None, latest=True)
    analyze_target, _ = _resolve_session_target(None, latest=True)

    assert list_row1 == open_target.stem == analyze_target.stem == "beta"


def test_ordering_stable_across_mtime_drift(tmp_path):
    """Re-touching the live session's mtime must not reorder the list."""
    trace_dir = tmp_path / "tr"
    trace_dir.mkdir()
    live = _write_session(trace_dir, "live", "2026-05-21T08:00:00.000Z", mtime=5_000)
    _write_session(trace_dir, "recent", "2026-05-21T08:30:00.000Z", mtime=4_000)

    before = [f.stem for f in _list_session_files(trace_dir)]
    # Simulate the live session getting appended again (mtime jumps).
    os.utime(live, (99_999_999, 99_999_999))
    after = [f.stem for f in _list_session_files(trace_dir)]
    assert before == after == ["recent", "live"]


def test_malformed_first_event_falls_back_to_mtime(tmp_path):
    """A file whose first line has no timestamp orders by mtime.

    In production, mtime and a parsed start-epoch are on the same scale
    (both ~now), so a malformed file's mtime is comparable to others'
    start times. Here 'good' starts ~2026 (epoch ~1.78e9); give 'bad' a
    clearly-later mtime so it should rank first via the mtime fallback.
    """
    trace_dir = tmp_path / "tr"
    trace_dir.mkdir()
    bad = trace_dir / "bad.jsonl"
    bad.write_text("not json\n", encoding="utf-8")
    os.utime(bad, (2_000_000_000, 2_000_000_000))  # ~year 2033
    _write_session(trace_dir, "good", "2026-05-21T08:00:00.000Z",
                   mtime=1_700_000_000)
    # bad has no parseable start → ranks by its (later) mtime, first.
    ordered = _list_session_files(trace_dir)
    assert ordered[0].stem == "bad"


# ---------------------------------------------------------------------------
# F-V10 — open accepts file paths like analyze
# ---------------------------------------------------------------------------


def test_open_accepts_file_path(tmp_path, monkeypatch, capsys):
    """`sentience open <path>` renders a trace given by file path."""
    # A trace file OUTSIDE any configured trace dir.
    other = tmp_path / "elsewhere"
    other.mkdir()
    f = _write_session(other, "path-session", "2026-05-21T08:00:00.000Z")
    # Empty trace dir so resolution can only succeed via the file path.
    empty = tmp_path / "tr"
    empty.mkdir()
    monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(empty))

    ns = argparse.Namespace(session_id=str(f), latest=False, summary=False)
    code = run_open(ns)
    out = capsys.readouterr().out
    assert code == 0
    assert "Session: path-session" in out


def test_open_and_analyze_accept_same_path(tmp_path):
    """Parity: the resolver returns the file for both subcommands' inputs."""
    f = _write_session(tmp_path, "shared", "2026-05-21T08:00:00.000Z")
    open_path, open_err = _resolve_session_target(str(f), latest=False)
    analyze_path, analyze_err = _resolve_session_target(str(f), latest=False)
    assert open_err is None and analyze_err is None
    assert open_path == analyze_path == f


def test_nonexistent_path_gives_file_not_found(tmp_path):
    bogus = str(tmp_path / "nope.jsonl")
    path, err = _resolve_session_target(bogus, latest=False)
    assert path is None
    assert "Trace file not found" in err
