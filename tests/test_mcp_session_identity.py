"""v0.3.0 CP3: current-session identification (plan §7).

Env primary + trace cross-check + bounded newest-mtime fallback + fail-closed,
with each defined branch tested. `now` and file mtimes are injected so the
freshness window is exercised deterministically.
"""

from __future__ import annotations

import os

from sentience_governor.mcp_server.session_identity import (
    ENV_SESSION_ID,
    FRESHNESS_WINDOW_SECONDS,
    NO_CURRENT_SESSION,
    NOT_YET_CAPTURED,
    RESOLVED,
    SOURCE_ENV,
    SOURCE_FALLBACK,
    WRITE_FRESHNESS_WINDOW_SECONDS,
    resolve_current_session,
)

NOW = 1_000_000.0  # fixed reference clock for all cases


def _trace(trace_dir, session_id, age_seconds):
    """Write a trace file and stamp its mtime to NOW - age_seconds."""
    path = trace_dir / f"{session_id}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    mtime = NOW - age_seconds
    os.utime(path, (mtime, mtime))
    return path


class TestEnvPrimaryCrossCheck:
    def test_env_id_with_fresh_trace_resolves(self, tmp_path):
        _trace(tmp_path, "sess-A", age_seconds=10)
        r = resolve_current_session(
            tmp_path, env={ENV_SESSION_ID: "sess-A"}, now=NOW
        )
        assert r.status == RESOLVED
        assert r.session_id == "sess-A"
        assert r.source == SOURCE_ENV
        assert r.writable() is True

    def test_env_id_with_no_trace_is_not_yet_captured(self, tmp_path):
        # Env says a session exists, but nothing captured it yet.
        r = resolve_current_session(
            tmp_path, env={ENV_SESSION_ID: "sess-A"}, now=NOW
        )
        assert r.status == NOT_YET_CAPTURED
        assert r.writable() is False
        assert r.session_id is None

    def test_env_id_with_stale_trace_fails_closed(self, tmp_path):
        _trace(tmp_path, "sess-A", age_seconds=FRESHNESS_WINDOW_SECONDS + 60)
        r = resolve_current_session(
            tmp_path, env={ENV_SESSION_ID: "sess-A"}, now=NOW
        )
        assert r.status == NO_CURRENT_SESSION
        assert r.writable() is False

    def test_env_id_conflicting_with_fresher_trace_fails_closed(self, tmp_path):
        # Candidate is fresh, but a *different* trace is fresher and active:
        # the env is possibly stale (server reuse) -> fail closed.
        _trace(tmp_path, "sess-A", age_seconds=300)
        _trace(tmp_path, "sess-B", age_seconds=5)
        r = resolve_current_session(
            tmp_path, env={ENV_SESSION_ID: "sess-A"}, now=NOW
        )
        assert r.status == NO_CURRENT_SESSION
        assert r.writable() is False
        assert "conflict" in r.reason

    def test_env_id_wins_when_the_other_trace_is_stale(self, tmp_path):
        # A different trace exists but is stale -> not a conflict; the fresh
        # candidate resolves.
        _trace(tmp_path, "sess-A", age_seconds=20)
        _trace(tmp_path, "sess-old", age_seconds=FRESHNESS_WINDOW_SECONDS + 500)
        r = resolve_current_session(
            tmp_path, env={ENV_SESSION_ID: "sess-A"}, now=NOW
        )
        assert r.status == RESOLVED
        assert r.session_id == "sess-A"


class TestFallbackAndNonClaudeLaunch:
    def test_no_env_one_fresh_trace_uses_bounded_fallback(self, tmp_path):
        _trace(tmp_path, "sess-A", age_seconds=30)
        r = resolve_current_session(tmp_path, env={}, now=NOW)
        assert r.status == RESOLVED
        assert r.session_id == "sess-A"
        assert r.source == SOURCE_FALLBACK

    def test_no_env_no_fresh_trace_is_no_current_session(self, tmp_path):
        _trace(tmp_path, "sess-A", age_seconds=FRESHNESS_WINDOW_SECONDS + 10)
        r = resolve_current_session(tmp_path, env={}, now=NOW)
        assert r.status == NO_CURRENT_SESSION
        assert r.writable() is False

    def test_no_env_multiple_fresh_traces_fail_closed(self, tmp_path):
        _trace(tmp_path, "sess-A", age_seconds=10)
        _trace(tmp_path, "sess-B", age_seconds=20)
        r = resolve_current_session(tmp_path, env={}, now=NOW)
        assert r.status == NO_CURRENT_SESSION
        assert r.writable() is False

    def test_no_env_empty_trace_dir_is_no_current_session(self, tmp_path):
        r = resolve_current_session(tmp_path, env={}, now=NOW)
        assert r.status == NO_CURRENT_SESSION
        assert r.writable() is False

    def test_missing_trace_dir_is_no_current_session(self, tmp_path):
        r = resolve_current_session(tmp_path / "nope", env={}, now=NOW)
        assert r.status == NO_CURRENT_SESSION


class TestFreshnessWindowParameter:
    """The read window is a UX tolerance; the write window is a safety gate.
    A candidate that is fresh for a read can be stale for a write."""

    def test_write_window_rejects_trace_inside_read_but_outside_write(
        self, tmp_path
    ):
        # 300s old: inside the 1800s read window, outside the 90s write window.
        assert 90 < 300 < FRESHNESS_WINDOW_SECONDS
        _trace(tmp_path, "sess-A", age_seconds=300)
        env = {ENV_SESSION_ID: "sess-A"}

        # Read/status (default window) still resolves.
        read = resolve_current_session(tmp_path, env=env, now=NOW)
        assert read.status == RESOLVED

        # The tight write window fails closed on the same trace.
        write = resolve_current_session(
            tmp_path, env=env, now=NOW,
            freshness_window=WRITE_FRESHNESS_WINDOW_SECONDS,
        )
        assert write.status == NO_CURRENT_SESSION
        assert write.writable() is False

    def test_stale_env_with_no_newer_trace_rejected_by_write_window(
        self, tmp_path
    ):
        # The exact stale-env race: an old env candidate, NO newer session
        # trace yet (no competitor), aged past the write window -> the write
        # window fails closed even though nothing contradicts the candidate.
        _trace(
            tmp_path, "old-sess",
            age_seconds=WRITE_FRESHNESS_WINDOW_SECONDS + 30,
        )
        r = resolve_current_session(
            tmp_path, env={ENV_SESSION_ID: "old-sess"}, now=NOW,
            freshness_window=WRITE_FRESHNESS_WINDOW_SECONDS,
        )
        assert r.status == NO_CURRENT_SESSION
        assert r.writable() is False

    def test_fresh_candidate_within_write_window_resolves_for_write(
        self, tmp_path
    ):
        _trace(tmp_path, "sess-A", age_seconds=30)  # within 90s
        r = resolve_current_session(
            tmp_path, env={ENV_SESSION_ID: "sess-A"}, now=NOW,
            freshness_window=WRITE_FRESHNESS_WINDOW_SECONDS,
        )
        assert r.status == RESOLVED
        assert r.session_id == "sess-A"


class TestChildSessionFlagIgnored:
    def test_child_session_flag_does_not_change_binding(self, tmp_path):
        # CLAUDE_CODE_CHILD_SESSION is informational; binding is by
        # CLAUDE_CODE_SESSION_ID (plan §7 defined behavior).
        _trace(tmp_path, "sess-A", age_seconds=10)
        env = {ENV_SESSION_ID: "sess-A", "CLAUDE_CODE_CHILD_SESSION": "1"}
        r = resolve_current_session(tmp_path, env=env, now=NOW)
        assert r.status == RESOLVED
        assert r.session_id == "sess-A"
