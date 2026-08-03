"""v0.3.0 CP1: Sentience MCP server skeleton + the two session-independent
reads (`sentience_explain`, `sentience_profile_view`).

The tool payloads are pure (no `mcp` dependency), so they are tested
directly. The server construction is tested behind `importorskip("mcp")`.
"""

from __future__ import annotations

import pytest

from sentience_governor.analyze.methodology import build_methodology
from sentience_governor.mcp_server import server as mcp_server
from sentience_governor.mcp_server.server import (
    SERVER_NAME,
    explain_payload,
    intent_payload,
    profile_view_payload,
    pulse_payload,
    session_status_payload,
    violations_payload,
)


def _all_keys(obj):
    """Every dict key appearing anywhere in a nested structure."""
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _all_keys(item)
    return keys


class TestExplainPayload:
    def test_is_the_methodology_dict(self):
        assert explain_payload() == build_methodology()

    def test_carries_the_token_classes_and_boundary(self):
        p = explain_payload()
        assert set(p["token_classes"]) == {
            "prompt", "completion", "cached_read", "cached_write",
        }
        # The per-turn (not per-tool) attribution boundary must be present.
        assert "metered per model turn, not per tool" in p["attribution_boundary"]


class TestProfileViewPayload:
    def test_shape(self):
        p = profile_view_payload()
        assert isinstance(p["profile"], dict)
        assert "schema_version" in p["profile"]
        for key in ("from_file", "source_path", "fingerprint", "schema_version"):
            assert key in p

    def test_defaults_when_no_profile_file(self, monkeypatch, tmp_path):
        # Point the loader at a non-existent path so it returns defaults.
        from sentience_governor.profile import loader
        monkeypatch.setattr(loader, "DEFAULT_PROFILE_PATH", tmp_path / "nope.yaml")
        p = profile_view_payload()
        assert p["from_file"] is False
        assert p["source_path"] is None
        # Defaults still produce a valid profile dict + fingerprint.
        assert isinstance(p["profile"], dict)
        assert isinstance(p["fingerprint"], str) and p["fingerprint"]

    def test_reads_a_real_profile_file(self, monkeypatch, tmp_path):
        from sentience_governor.profile import loader
        pf = tmp_path / "profile.yaml"
        pf.write_text("schema_version: 1\n", encoding="utf-8")
        monkeypatch.setattr(loader, "DEFAULT_PROFILE_PATH", pf)
        p = profile_view_payload()
        assert p["from_file"] is True
        assert p["source_path"] == str(pf)


class TestLastCompletedSessionResolver:
    """CP2: measured reads operate on the last COMPLETED session, never the
    live one, and name the session they read."""

    def test_resolver_returns_session_events_and_end_time(
        self, monkeypatch, tmp_path
    ):
        from sentience_governor.cli import ux
        (tmp_path / "sess-abc.jsonl").write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(ux, "_resolve_trace_dir", lambda: tmp_path)
        monkeypatch.setattr(
            ux, "_latest_token_bearing_session", lambda d, exclude: ("sess-abc", 3)
        )
        monkeypatch.setattr(
            ux, "_load_session", lambda p: ("sess-abc", [{"event": 1}])
        )
        resolved = mcp_server._resolve_last_completed_session()
        assert resolved is not None
        sid, events, end_iso = resolved
        assert sid == "sess-abc"
        assert events == [{"event": 1}]
        assert end_iso  # ISO 8601 string derived from the file's mtime

    def test_resolver_excludes_the_live_session_id(self, monkeypatch, tmp_path):
        from sentience_governor.cli import ux
        seen = {}

        def _fake_latest(trace_dir, exclude):
            seen["exclude"] = exclude
            return None

        monkeypatch.setattr(ux, "_resolve_trace_dir", lambda: tmp_path)
        monkeypatch.setattr(ux, "_latest_token_bearing_session", _fake_latest)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "live-session-xyz")
        assert mcp_server._resolve_last_completed_session() is None
        assert seen["exclude"] == "live-session-xyz"

    def test_resolver_none_when_no_token_bearing_session(
        self, monkeypatch, tmp_path
    ):
        from sentience_governor.cli import ux
        monkeypatch.setattr(ux, "_resolve_trace_dir", lambda: tmp_path)
        monkeypatch.setattr(
            ux, "_latest_token_bearing_session", lambda d, exclude: None
        )
        assert mcp_server._resolve_last_completed_session() is None


class TestMeasuredReadPayloads:
    """CP2: pulse / intent / violations wrap the analyzers and name the
    session; no-completed-session is a first-class status."""

    def test_payloads_name_session_and_wrap_analyzer(self, monkeypatch):
        events = [{"event_type": "AGENT_REGISTERED"}]
        monkeypatch.setattr(
            mcp_server,
            "_resolve_last_completed_session",
            lambda: ("sess-42", events, "2026-07-07T00:00:00+00:00"),
        )
        from sentience_governor.analyze.pulse import compute_pulse

        p = pulse_payload()
        assert p["session_id"] == "sess-42"
        assert p["session_end"] == "2026-07-07T00:00:00+00:00"
        assert p["result"] == compute_pulse(events)
        # intent + violations follow the same envelope.
        assert intent_payload()["session_id"] == "sess-42"
        assert violations_payload()["session_id"] == "sess-42"

    def test_no_completed_session_is_a_status(self, monkeypatch):
        monkeypatch.setattr(
            mcp_server, "_resolve_last_completed_session", lambda: None
        )
        for payload in (pulse_payload, intent_payload, violations_payload):
            out = payload()
            assert out["status"] == "no_completed_session"
            assert "session_id" not in out
            assert out["detail"]


class TestSessionStatusPayload:
    """CP4: structural-only status of the live session, fail-closed, with the
    'no live token claims' contract invariant enforced by test."""

    def _fresh_current_session(self, monkeypatch, tmp_path):
        """Point the payload at a controlled trace dir with one fresh trace
        that the env id selects, and return (session_id, events)."""
        from sentience_governor.cli import ux

        events = [
            {"event_type": "AGENT_REGISTERED", "payload": {}},
            {
                "event_type": "SCOPE_ASSERTED",
                "payload": {"operation_type": "read", "tool_id": "Read"},
            },
            {
                "event_type": "SCOPE_ASSERTED",
                "payload": {"operation_type": "execute", "tool_id": "Bash"},
            },
        ]
        sid = "live-sess-1"
        (tmp_path / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(ux, "_resolve_trace_dir", lambda: tmp_path)
        monkeypatch.setattr(ux, "_load_session", lambda p: (sid, events))
        return sid, events

    def test_structural_status_of_the_live_session(self, monkeypatch, tmp_path):
        sid, events = self._fresh_current_session(monkeypatch, tmp_path)
        out = session_status_payload(env={"CLAUDE_CODE_SESSION_ID": sid})
        assert out["status"] == "current_session"
        assert out["session_id"] == sid
        assert out["event_count"] == len(events)
        assert out["partial"] is True
        assert out["token_analysis"] == "unavailable until SessionEnd"
        # Structural tool-call block present.
        assert set(out["tool_calls"]) == {"total", "by_operation", "by_tool"}
        assert out["tool_calls"]["total"] == 2
        assert "policy_violations_so_far" in out
        assert "advisory_flags_so_far" in out

    def test_no_live_token_claims_invariant(self, monkeypatch, tmp_path):
        sid, _ = self._fresh_current_session(monkeypatch, tmp_path)
        out = session_status_payload(env={"CLAUDE_CODE_SESSION_ID": sid})
        keys = _all_keys(out)
        # The ONLY key mentioning "token" may be the disclaimer field.
        assert {k for k in keys if "token" in k.lower()} == {"token_analysis"}
        # No burn / economics / pulse / attribution surfaces anywhere.
        forbidden = {
            "tool_token_attribution",
            "total_tokens",
            "undeclared_tokens",
            "burn_rate",
            "economics",
            "pulse",
            "token_breakdown",
        }
        assert forbidden.isdisjoint(keys)

    def test_fails_closed_when_no_live_session(self, monkeypatch, tmp_path):
        from sentience_governor.cli import ux

        monkeypatch.setattr(ux, "_resolve_trace_dir", lambda: tmp_path)
        # No env id and an empty trace dir -> no current session.
        out = session_status_payload(env={})
        assert out["status"] == "no_current_session"
        assert "session_id" not in out
        assert "tool_calls" not in out
        # Still explicit about token analysis + partial framing.
        assert out["token_analysis"] == "unavailable until SessionEnd"

    def test_not_yet_captured_returns_no_counts(self, monkeypatch, tmp_path):
        from sentience_governor.cli import ux

        monkeypatch.setattr(ux, "_resolve_trace_dir", lambda: tmp_path)
        # Env names a session but nothing captured it yet.
        out = session_status_payload(env={"CLAUDE_CODE_SESSION_ID": "ghost"})
        assert out["status"] == "not_yet_captured"
        assert "tool_calls" not in out


class TestServerConstruction:
    """Behind importorskip: the optional `mcp` package is required to build
    the actual FastMCP server."""

    def test_build_server_registers_the_tools(self):
        pytest.importorskip("mcp")
        from sentience_governor.mcp_server.server import build_server
        server = build_server()
        names = {t.name for t in server._tool_manager.list_tools()}
        assert {
            "sentience_explain",
            "sentience_profile_view",
            "sentience_pulse",
            "sentience_intent",
            "sentience_violations",
            "sentience_session_status",
            "sentience_declare_intent",
        } <= names
        assert server.name == SERVER_NAME
