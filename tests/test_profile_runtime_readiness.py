"""Runtime readiness on the binding path (core 0.3.2.1, issue #19).

The malformed-profile corpus (tests/test_profile_invalid_corpus.py) pins the
outcome for every fixture. These tests pin the mechanisms around it that the
corpus does not reach directly:

* the constructor names the key path when it rejects a non-string key;
* the resolver reports a constructor ``TypeError`` as the load failure it is;
* ``session_start`` never raises, even for an object it cannot assess;
* the Claude Code hook re-resolves when a snapshot written by an earlier
  release rebuilds into a profile the validator now rejects, and leaves the
  snapshot file alone;
* the MCP wrapper and the LangChain handler open their session ungoverned,
  with a warning and a truthful registration, when the default profile is
  malformed. Before 0.3.2.1 the wrapper raised into ``async with`` and the
  handler failed silently.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.profile import GovernanceProfile
from sentience_governor.profile import loader as loader_module
from sentience_governor.profile.resolver import (
    SOURCE_BOUND,
    SOURCE_DEGRADED,
    SOURCE_NONE,
    ResolvedProfile,
    resolve_profile,
)
from sentience_governor.schema.events import GovernanceEvent
from sentience_governor.session_manager import resumption as res
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.session_manager.resumption import read_session_binding
from sentience_governor.sink.writer import SinkWriter
from sentience_governor.wrapper import claude_code_hook as cch
from sentience_governor.wrapper.claude_code_hook import ClaudeCodeGovernanceHook
from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler
from sentience_governor.wrapper.mcp import MCPClientLike, wrap_mcp_client

VALID = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  demand_at: first_write\n"
    "task_boundary:\n"
    "  signals: [dir_change]\n"
    "high_consequence:\n"
    "  tools: ['Bash:shell']\n"
)
INVALID_TOOLS = "schema_version: 1\nhigh_consequence:\n  tools: [1, 2]\n"
UNPARSEABLE = "- not\n- a mapping\n"


# ---------------------------------------------------------------------------
# D9: the constructor names the key path
# ---------------------------------------------------------------------------


class TestConstructorAdmissibility:
    def test_top_level_non_string_key_names_root(self):
        with pytest.raises(ValueError) as exc:
            GovernanceProfile({"schema_version": 1, 1: "x"})
        assert "int key 1 at <root>" in str(exc.value)

    def test_nested_key_path_is_dotted(self):
        with pytest.raises(ValueError) as exc:
            GovernanceProfile({"session_intent": {True: "x"}})
        assert "bool key True at session_intent" in str(exc.value)

    def test_key_inside_a_list_element_is_indexed(self):
        data = {"high_consequence": {"operations": [{"match": "x", 3: "y"}]}}
        with pytest.raises(ValueError) as exc:
            GovernanceProfile(data)
        assert "at high_consequence.operations[0]" in str(exc.value)

    def test_null_key_is_rejected_not_coerced(self):
        with pytest.raises(ValueError) as exc:
            GovernanceProfile({None: "x"})
        assert "NoneType key None" in str(exc.value)

    def test_unserializable_value_still_raises_type_error(self):
        import datetime

        with pytest.raises(TypeError):
            GovernanceProfile({"schema_version": datetime.date(2026, 1, 1)})

    def test_string_keyed_profile_is_unchanged(self):
        # Same data, same fingerprint, whether or not the check ran: the
        # check does not touch admissible input.
        data = {"schema_version": 1, "session_intent": {"demand_at": "first_write"}}
        assert GovernanceProfile(data).fingerprint() == GovernanceProfile(dict(data)).fingerprint()
        assert GovernanceProfile(data).to_dict()["session_intent"] == {"demand_at": "first_write"}


# ---------------------------------------------------------------------------
# Resolver: a constructor TypeError is a load failure, reported as one
# ---------------------------------------------------------------------------


def _bind(tmp_path: Path, profile_text: str, default_text: str = VALID) -> ResolvedProfile:
    target = tmp_path / "bound.yaml"
    target.write_text(profile_text, encoding="utf-8")
    resolution = tmp_path / "resolution.yaml"
    resolution.write_text(
        f"schema_version: 1\nbindings:\n  - agent_id: 'a-*'\n    profile: {target}\n",
        encoding="utf-8",
    )
    default = tmp_path / "profile.yaml"
    default.write_text(default_text, encoding="utf-8")
    return resolve_profile(agent_id="a-1", resolution_path=resolution, default_path=default)


class TestResolverReadiness:
    def test_bound_profile_with_a_date_value_is_a_reported_load_failure(self, tmp_path: Path):
        r = _bind(tmp_path, "schema_version: 1\nsession_intent:\n  demand_at: 2026-01-01\n")
        assert r.source == SOURCE_DEGRADED and r.profile is not None
        assert len(r.warnings) == 1
        assert "could not be loaded" in r.warnings[0] and "TypeError" in r.warnings[0]
        assert "unexpectedly" not in r.warnings[0]

    def test_bound_invalid_profile_warning_names_file_and_field(self, tmp_path: Path):
        r = _bind(tmp_path, INVALID_TOOLS)
        assert r.source == SOURCE_DEGRADED and r.binding == "a-*"
        assert r.profile is not None and r.profile.source_path == tmp_path / "profile.yaml"
        assert len(r.warnings) == 1
        assert "bound.yaml" in r.warnings[0] and "high_consequence.tools" in r.warnings[0]

    def test_valid_bound_profile_is_bound_with_no_warnings(self, tmp_path: Path):
        r = _bind(tmp_path, VALID, default_text=INVALID_TOOLS)
        assert r.source == SOURCE_BOUND and r.warnings == ()
        assert r.profile is not None and r.profile.source_path == tmp_path / "bound.yaml"

    def test_absent_default_path_argument_uses_the_loader_default_and_fails_open(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        bad = tmp_path / "profile.yaml"
        bad.write_text(UNPARSEABLE, encoding="utf-8")
        monkeypatch.setattr(loader_module, "DEFAULT_PROFILE_PATH", bad)
        monkeypatch.setattr(loader_module, "DEFAULT_RESOLUTION_PATH", tmp_path / "absent.yaml")
        with caplog.at_level(logging.WARNING, logger="sentience_governor.profile.resolver"):
            r = resolve_profile(agent_id="anyone")
        assert r.source == SOURCE_NONE and r.profile is None
        assert len(r.warnings) == 1 and str(bad) in r.warnings[0]
        assert sum("profile resolution" in rec.getMessage() for rec in caplog.records) == 1


# ---------------------------------------------------------------------------
# session_start: refuses, warns once, never raises
# ---------------------------------------------------------------------------


class TestSessionStartReadiness:
    def test_valid_profile_is_stored_by_reference(self):
        profile = GovernanceProfile.defaults()
        sm = SessionManager()
        sm.session_start("s-ok", "agent", profile=profile)
        assert sm.get_profile("s-ok") is profile

    def test_invalid_profile_is_refused_with_one_warning_naming_the_source(self, tmp_path: Path, caplog):
        path = tmp_path / "bad.yaml"
        path.write_text(INVALID_TOOLS, encoding="utf-8")
        profile = GovernanceProfile.from_file(path)
        sm = SessionManager()
        with caplog.at_level(logging.WARNING):
            sm.session_start("s-bad", "agent", profile=profile)
        assert sm.get_profile("s-bad") is None
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "s-bad" in warnings[0] and str(path) in warnings[0] and "high_consequence.tools" in warnings[0]

    def test_object_that_cannot_be_assessed_is_refused_not_raised(self, caplog):
        sm = SessionManager()
        with caplog.at_level(logging.WARNING):
            sm.session_start("s-odd", "agent", profile=object())
        assert sm.get_profile("s-odd") is None
        assert sum("s-odd" in r.getMessage() for r in caplog.records) == 1

    def test_refusal_does_not_disturb_collision_handling(self, tmp_path: Path):
        path = tmp_path / "bad.yaml"
        path.write_text(INVALID_TOOLS, encoding="utf-8")
        sm = SessionManager()
        sm.session_start("s-first", "agent", profile=GovernanceProfile.defaults())
        sm.session_start("s-second", "agent", profile=GovernanceProfile.from_file(path))
        assert sm.get_state("s-first").name == "CLOSED"
        assert sm.get_state("s-second").name == "ACTIVE"
        assert sm.get_profile("s-second") is None


# ---------------------------------------------------------------------------
# Claude Code hook: a historical snapshot that is no longer runtime-ready
# ---------------------------------------------------------------------------

S1 = "sess-ready-0001-aaaa"


class _Env:
    def __init__(self, home: Path, sink_base: Path):
        self.home = home
        self.sink_base = sink_base
        self.profiles = home / "profiles"
        self.resolution = home / "resolution.yaml"
        self.default = home / "profile.yaml"

    def sink_for(self, session_id: str) -> Path:
        return cch._session_file_for(self.sink_base, False, session_id)


@pytest.fixture
def env(tmp_path: Path, monkeypatch) -> _Env:
    home = tmp_path / "home"
    (home / "profiles").mkdir(parents=True)
    e = _Env(home, tmp_path / "traces")
    (e.profiles / "A.yaml").write_text(VALID, encoding="utf-8")
    e.default.write_text(VALID.replace("first_write", "never"), encoding="utf-8")
    e.resolution.write_text(
        "schema_version: 1\nbindings:\n  - agent_id: claude-code-*\n    profile: profiles/A.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader_module, "DEFAULT_RESOLUTION_PATH", e.resolution)
    monkeypatch.setattr(loader_module, "DEFAULT_PROFILE_PATH", e.default)
    monkeypatch.delenv("SENTIENCE_CLAUDE_CODE_AGENT_ID_PREFIX", raising=False)
    monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(e.sink_base))
    monkeypatch.setattr(cch, "_FALLBACK_SINK_DIR", tmp_path / "fallback")
    return e


def _pre(session: str, use_id: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/x.txt", "content": "hi"},
        "tool_use_id": use_id,
        "cwd": "/tmp",
    }


def _events(sink: Path) -> List[dict]:
    if not sink.exists():
        return []
    return [json.loads(l) for l in sink.read_text(encoding="utf-8").splitlines() if l.strip()]


def _plant_historical_invalid_binding(env: _Env, session_id: str) -> Path:
    """Write the binding and snapshot exactly as 0.3.2 did for a profile the
    validator now rejects (the constructor accepts string-keyed data without
    validating, which is the seam an earlier release bound through)."""
    invalid = GovernanceProfile(
        {"schema_version": 1, "high_consequence": {"tools": [1, 2]}},
        source_path=env.profiles / "historical.yaml",
    )
    fresh = ResolvedProfile(profile=invalid, source=SOURCE_BOUND, binding="claude-code-*", warnings=())
    sink = env.sink_for(session_id)
    with res.sink_lock(sink):
        cch._establish_binding(sink, session_id, fresh, recovered_from=None)
    entry = read_session_binding(sink, session_id)
    assert entry is not None and entry["profile_content_hash"] == invalid.content_hash()
    snapshot = sink.parent / entry["snapshot"]
    assert snapshot.is_file()
    return snapshot


class TestHookRehydration:
    def test_first_process_after_upgrade_re_resolves_and_keeps_the_snapshot(self, env: _Env, caplog):
        snapshot = _plant_historical_invalid_binding(env, S1)
        fa = GovernanceProfile.from_file(env.profiles / "A.yaml").fingerprint()
        with caplog.at_level(logging.WARNING, logger="sentience_governor.wrapper.claude_code_hook"):
            ClaudeCodeGovernanceHook(_pre(S1, "use-1"), sink_base=env.sink_base).process()
        events = _events(env.sink_for(S1))
        regs = [e for e in events if e["event_type"] == "AGENT_REGISTERED"]
        assert len(regs) == 1
        assert regs[0]["profile_fingerprint"] == fa
        assert regs[0]["payload"]["profile_resolution"] == SOURCE_BOUND
        entry = read_session_binding(env.sink_for(S1), S1)
        assert entry["profile_fingerprint"] == fa and entry["recovered_from"] is None
        assert snapshot.is_file()  # history is kept, not rewritten or deleted
        notices = [r.getMessage() for r in caplog.records if "not runtime-ready" in r.getMessage()]
        assert len(notices) == 1 and "historical.yaml" in notices[0]

    def test_later_process_with_a_registration_rematerializes_on_the_fresh_profile(self, env: _Env):
        fa = GovernanceProfile.from_file(env.profiles / "A.yaml").fingerprint()
        ClaudeCodeGovernanceHook(_pre(S1, "use-1"), sink_base=env.sink_base).process()
        before = len(_events(env.sink_for(S1)))
        snapshot = _plant_historical_invalid_binding(env, S1)  # binding now disagrees with history
        ClaudeCodeGovernanceHook(_pre(S1, "use-2"), sink_base=env.sink_base).process()
        events = _events(env.sink_for(S1))
        assert len(events) > before
        assert len([e for e in events if e["event_type"] == "AGENT_REGISTERED"]) == 1
        assert {e["profile_fingerprint"] for e in events if e.get("profile_fingerprint")} == {fa}
        entry = read_session_binding(env.sink_for(S1), S1)
        assert entry["profile_fingerprint"] == fa
        assert entry["recovered_from"] == cch._RECOVERED_REMATERIALIZED
        assert snapshot.is_file()

    def test_invalid_bound_file_degrades_to_the_default_in_the_hook(self, env: _Env):
        (env.profiles / "A.yaml").write_text(INVALID_TOOLS, encoding="utf-8")
        fd = GovernanceProfile.from_file(env.default).fingerprint()
        ClaudeCodeGovernanceHook(_pre(S1, "use-1"), sink_base=env.sink_base).process()
        regs = [e for e in _events(env.sink_for(S1)) if e["event_type"] == "AGENT_REGISTERED"]
        assert len(regs) == 1
        assert regs[0]["profile_fingerprint"] == fd
        assert regs[0]["payload"]["profile_resolution"] == SOURCE_DEGRADED
        assert regs[0]["payload"]["profile_binding"] == "claude-code-*"


# ---------------------------------------------------------------------------
# Adapters: a malformed default no longer raises or fails silently
# ---------------------------------------------------------------------------


class _CapturingSink:
    def __init__(self) -> None:
        self.events: List[GovernanceEvent] = []

    def write(self, event: GovernanceEvent) -> bool:
        self.events.append(event)
        return True


@pytest.fixture(params=["unparseable", "invalid"])
def malformed_default(request, tmp_path: Path, monkeypatch) -> Path:
    bad = tmp_path / "profile.yaml"
    bad.write_text(UNPARSEABLE if request.param == "unparseable" else INVALID_TOOLS, encoding="utf-8")
    monkeypatch.setattr(loader_module, "DEFAULT_PROFILE_PATH", bad)
    monkeypatch.setattr(loader_module, "DEFAULT_RESOLUTION_PATH", tmp_path / "absent.yaml")
    return bad


def _registration(events: List[GovernanceEvent]) -> GovernanceEvent:
    regs = [e for e in events if e.event_type.value == "AGENT_REGISTERED"]
    assert len(regs) == 1
    return regs[0]


class TestAdapterPaths:
    @pytest.mark.asyncio
    async def test_mcp_wrapper_opens_ungoverned_with_a_warning(self, malformed_default: Path, caplog):
        captured = _CapturingSink()

        class FakeMCPClient(MCPClientLike):
            def send_tool_call(self, tool_name: str, arguments: dict) -> Any:
                return {"ok": True}

        sm = SessionManager()
        wrapped = wrap_mcp_client(
            target=FakeMCPClient(),
            session_manager=sm,
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            agent_id="mcp-agent",
            stated_objective="run",
            declared_capabilities=["test.read"],
            session_id="sess-mcp-ready",
        )
        with caplog.at_level(logging.WARNING, logger="sentience_governor.profile.resolver"):
            async with wrapped:
                pass
        reg = _registration(captured.events)
        assert reg.payload.profile_loaded is None
        assert reg.profile_fingerprint is None
        assert sm.get_profile("sess-mcp-ready") is None
        assert sum(str(malformed_default) in r.getMessage() for r in caplog.records) == 1

    def test_langchain_handler_opens_ungoverned_with_a_warning(self, malformed_default: Path, caplog):
        captured = _CapturingSink()
        sm = SessionManager()
        handler = SentienceCallbackHandler(
            agent_id="lc-agent",
            session_manager=sm,
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            agent_version="1.0.0",
            vendor_id="v",
            declared_capabilities=["test.read"],
            owner_claim="user",
        )
        with caplog.at_level(logging.WARNING, logger="sentience_governor.profile.resolver"):
            handler.on_chain_start({"name": "chain"}, {"input": "hello"})
        reg = _registration(captured.events)
        assert reg.payload.profile_loaded is None
        assert reg.profile_fingerprint is None
        assert sum(str(malformed_default) in r.getMessage() for r in caplog.records) == 1
