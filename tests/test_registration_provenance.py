"""v0.3.2 CP3: registration provenance on AGENT_REGISTERED.

``profile_resolution`` and ``profile_binding`` are optional, None-omitted
payload fields recorded only when a session resolved through
``~/.sentience/resolution.yaml`` (bound or degraded). Sessions on the
machine default or on no profile omit both, so their registrations are
byte-identical to v0.3.1.2. The envelope ``profile_fingerprint`` is
unchanged, and the 64-hex content hash never appears in public evidence.

Recording provenance activates the provenance-aware recovery rule that
CP2 implemented and tested with synthetic registrations: silent
re-materialization now requires fingerprint AND recorded provenance
agreement; identical policy bytes reached through a different binding is
a visible re-resolve; pre-v0.3.2 registrations without provenance keep
fingerprint-only recovery; and a re-resolved binding is a sticky recovery
state, not a recovery loop.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import List

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder import builder as builder_module
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile import GovernanceProfile
from sentience_governor.profile import loader as loader_module
from sentience_governor.profile.resolver import (
    SOURCE_BOUND,
    SOURCE_DEFAULT,
    SOURCE_DEGRADED,
    SOURCE_NONE,
)
from sentience_governor.schema.events import (
    AgentRegisteredPayload,
    DeploymentMode,
    EventType,
    GovernanceEvent,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.session_manager.resumption import (
    read_registration,
    read_session_binding,
    sidecar_path_for,
)
from sentience_governor.sink.writer import SinkWriter
from tests.test_claude_code_sticky_binding import (
    PROFILE_A,
    PROFILE_B,
    PROFILE_D,
    S1,
    S2,
    Env,
    _Client,
    _CapturingSink,
    _events,
    _fingerprints,
    _post,
    _pre,
    _registrations,
    _run,
    _warnings,
    env,  # noqa: F401  (fixture re-export)
)

HEX64 = re.compile(r"[0-9a-f]{64}")


# ---------------------------------------------------------------------------
# schema and builder
# ---------------------------------------------------------------------------


def _profile_from_disk(tmp_path: Path, text: str = PROFILE_A) -> GovernanceProfile:
    p = tmp_path / "profile.yaml"
    p.write_text(text, encoding="utf-8")
    return GovernanceProfile.from_file(p)


def _registered(profile, **builder_kwargs) -> dict:
    sm = SessionManager()
    cache = InProcessCache()
    sm.session_start(session_id="sess", agent_id="agent", profile=profile)
    cache.init_session("sess")
    builder = EventBuilder(
        session_manager=sm,
        cache=cache,
        agent_id="agent",
        session_id="sess",
        deployment_mode=DeploymentMode.vendor_managed,
        **builder_kwargs,
    )
    event = builder.build_agent_registered(
        agent_version="1.0", vendor_id="v", declared_capabilities=["fs.read"], owner_claim="o"
    )
    return event.to_dict()


class TestSchema:
    def test_fields_optional_and_none_omitted(self):
        payload = AgentRegisteredPayload(
            agent_id="a", deployment_mode=DeploymentMode.vendor_managed
        )
        dumped = payload.model_dump()
        assert "profile_resolution" not in dumped and "profile_binding" not in dumped

    def test_fields_serialize_when_set(self):
        payload = AgentRegisteredPayload(
            agent_id="a",
            deployment_mode=DeploymentMode.vendor_managed,
            profile_resolution="bound",
            profile_binding="deploy-*",
        )
        dumped = payload.model_dump()
        assert dumped["profile_resolution"] == "bound"
        assert dumped["profile_binding"] == "deploy-*"

    def test_recorded_resolutions_match_resolver_constants(self):
        assert builder_module.PROVENANCE_RECORDED_RESOLUTIONS == {SOURCE_BOUND, SOURCE_DEGRADED}
        assert SOURCE_DEFAULT not in builder_module.PROVENANCE_RECORDED_RESOLUTIONS
        assert SOURCE_NONE not in builder_module.PROVENANCE_RECORDED_RESOLUTIONS


class TestBuilder:
    def test_bound_records_resolution_and_pattern(self, tmp_path: Path):
        prof = _profile_from_disk(tmp_path)
        dumped = _registered(prof, profile_resolution=SOURCE_BOUND, profile_binding="deploy-*")
        assert dumped["payload"]["profile_resolution"] == "bound"
        assert dumped["payload"]["profile_binding"] == "deploy-*"
        assert dumped["payload"]["profile_loaded"] is True
        assert dumped["profile_fingerprint"] == prof.fingerprint()

    def test_degraded_records_resolution_and_pattern(self, tmp_path: Path):
        prof = _profile_from_disk(tmp_path, PROFILE_D)
        dumped = _registered(prof, profile_resolution=SOURCE_DEGRADED, profile_binding="deploy-*")
        assert dumped["payload"]["profile_resolution"] == "degraded"
        assert dumped["payload"]["profile_binding"] == "deploy-*"
        assert dumped["profile_fingerprint"] == prof.fingerprint()

    def test_default_omits_provenance(self, tmp_path: Path):
        prof = _profile_from_disk(tmp_path)
        dumped = _registered(prof, profile_resolution=SOURCE_DEFAULT, profile_binding=None)
        assert "profile_resolution" not in dumped["payload"]
        assert "profile_binding" not in dumped["payload"]
        assert dumped["profile_fingerprint"] == prof.fingerprint()  # envelope unchanged

    def test_none_omits_provenance_and_matches_pre_profile_shape(self):
        with_none = _registered(None, profile_resolution=SOURCE_NONE, profile_binding=None)
        legacy = _registered(None)
        for dumped in (with_none, legacy):
            assert "profile_resolution" not in dumped["payload"]
            assert "profile_binding" not in dumped["payload"]
            assert "profile_fingerprint" not in dumped
        assert set(with_none["payload"]) == set(legacy["payload"])

    def test_bound_without_pattern_is_not_recorded_as_a_pattern(self, tmp_path: Path):
        """A caller passing bound with no pattern records resolution only;
        the binding field stays omitted rather than inventing a value."""
        prof = _profile_from_disk(tmp_path)
        dumped = _registered(prof, profile_resolution=SOURCE_BOUND, profile_binding=None)
        assert dumped["payload"]["profile_resolution"] == "bound"
        assert "profile_binding" not in dumped["payload"]

    def test_no_full_hash_in_serialized_event(self, tmp_path: Path):
        prof = _profile_from_disk(tmp_path)
        dumped = _registered(prof, profile_resolution=SOURCE_BOUND, profile_binding="deploy-*")
        assert not HEX64.search(json.dumps(dumped))


# ---------------------------------------------------------------------------
# adapters (one process per session)
# ---------------------------------------------------------------------------


class TestAdapters:
    @pytest.mark.asyncio
    async def test_mcp_bound_registration_carries_provenance(self, env: Env):
        from sentience_governor.wrapper.mcp import SentienceMCPAdapter, wrap_mcp_client

        env.write_resolution(("mcp-agent-*", "profiles/B.yaml"))
        captured = _CapturingSink()
        wrapped = wrap_mcp_client(
            target=SentienceMCPAdapter(delegate=_Client(), call_fn=lambda c, n, a: c.invoke(n, a)),
            session_manager=SessionManager(),
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            agent_id="mcp-agent-1",
            declared_capabilities=["crm.read"],
        )
        async with wrapped:
            pass
        reg = captured.events[0].to_dict()
        assert reg["event_type"] == EventType.AGENT_REGISTERED.value
        assert reg["payload"]["profile_resolution"] == "bound"
        assert reg["payload"]["profile_binding"] == "mcp-agent-*"
        assert reg["profile_fingerprint"] == env.fingerprint_of("B.yaml")

    @pytest.mark.asyncio
    async def test_mcp_default_registration_omits_provenance(self, env: Env):
        from sentience_governor.wrapper.mcp import SentienceMCPAdapter, wrap_mcp_client

        env.default.write_text(PROFILE_D, encoding="utf-8")
        captured = _CapturingSink()
        wrapped = wrap_mcp_client(
            target=SentienceMCPAdapter(delegate=_Client(), call_fn=lambda c, n, a: c.invoke(n, a)),
            session_manager=SessionManager(),
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            agent_id="unbound-agent",
            declared_capabilities=["crm.read"],
        )
        async with wrapped:
            pass
        reg = captured.events[0].to_dict()
        assert "profile_resolution" not in reg["payload"]
        assert "profile_binding" not in reg["payload"]
        assert reg["profile_fingerprint"] == GovernanceProfile.from_file(env.default).fingerprint()

    def test_langchain_degraded_registration_carries_provenance(self, env: Env):
        from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler

        env.default.write_text(PROFILE_D, encoding="utf-8")
        env.write_resolution(("lc-agent", "profiles/missing.yaml"))
        captured = _CapturingSink()
        handler = SentienceCallbackHandler(
            agent_id="lc-agent",
            session_manager=SessionManager(),
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            declared_capabilities=["test.read"],
        )
        handler.on_chain_start({}, {"input": "go"})
        reg = captured.events[0].to_dict()
        assert reg["payload"]["profile_resolution"] == "degraded"
        assert reg["payload"]["profile_binding"] == "lc-agent"
        assert reg["profile_fingerprint"] == GovernanceProfile.from_file(env.default).fingerprint()

    def test_langchain_none_registration_unchanged(self, env: Env):
        from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler

        env.resolution.unlink()
        captured = _CapturingSink()
        handler = SentienceCallbackHandler(
            agent_id="lc-agent",
            session_manager=SessionManager(),
            cache=InProcessCache(),
            sink_writer=SinkWriter(captured),
            declared_capabilities=["test.read"],
        )
        handler.on_chain_start({}, {"input": "go"})
        reg = captured.events[0].to_dict()
        assert "profile_resolution" not in reg["payload"]
        assert "profile_fingerprint" not in reg


# ---------------------------------------------------------------------------
# real hook processes: provenance recorded, and the recovery rule it activates
# ---------------------------------------------------------------------------


def _strip_provenance(sink: Path, session_id: str) -> None:
    """Rewrite the session's AGENT_REGISTERED as a 0.3.1.2 runtime wrote
    it (no provenance fields). Test-only simulation of an in-flight
    upgrade; the runtime never edits a trace."""
    lines = sink.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        event = json.loads(line)
        if event["event_type"] == EventType.AGENT_REGISTERED.value and event["session_id"] == session_id:
            event["payload"].pop("profile_resolution", None)
            event["payload"].pop("profile_binding", None)
            line = json.dumps(event)
        out.append(line)
    sink.write_text("".join(l + "\n" for l in out), encoding="utf-8")


class TestHookProvenance:
    def test_bound_session_registers_with_provenance(self, env: Env):
        _run(env, _pre(S1, "use-1"))
        _run(env, _post(S1, "use-1"))
        sink = env.sink_for(S1)
        reg = _registrations(_events(sink))[0]
        assert reg["payload"]["profile_resolution"] == "bound"
        assert reg["payload"]["profile_binding"] == "claude-code-*"
        assert reg["profile_fingerprint"] == env.fingerprint_of("A.yaml")
        r = read_registration(sink, S1)
        assert r.has_provenance and (r.profile_resolution, r.profile_binding) == ("bound", "claude-code-*")
        # Non-registration events carry the fingerprint only.
        for e in _events(sink)[1:]:
            assert "profile_resolution" not in e.get("payload", {})
        assert not HEX64.search(sink.read_text(encoding="utf-8"))

    def test_degraded_session_registers_with_provenance(self, env: Env):
        env.default.write_text(PROFILE_D, encoding="utf-8")
        env.write_resolution(("claude-code-*", "profiles/missing.yaml"))
        _run(env, _pre(S1))
        reg = _registrations(_events(env.sink_for(S1)))[0]
        assert reg["payload"]["profile_resolution"] == "degraded"
        assert reg["payload"]["profile_binding"] == "claude-code-*"
        assert reg["profile_fingerprint"] == GovernanceProfile.from_file(env.default).fingerprint()

    def test_default_and_none_sessions_omit_provenance(self, env: Env):
        env.resolution.unlink()
        _run(env, _pre(S1))  # none
        reg = _registrations(_events(env.sink_for(S1)))[0]
        assert "profile_resolution" not in reg["payload"] and "profile_fingerprint" not in reg
        env.default.write_text(PROFILE_D, encoding="utf-8")
        _run(env, _pre(S2))  # default
        reg = _registrations(_events(env.sink_for(S2)))[0]
        assert "profile_resolution" not in reg["payload"]
        assert "profile_binding" not in reg["payload"]
        assert reg["profile_fingerprint"] == GovernanceProfile.from_file(env.default).fingerprint()

    def test_12c_lost_sidecar_agreeing_provenance_rematerializes_silently(self, env: Env, caplog):
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        sidecar_path_for(sink).unlink()
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _run(env, _post(S1, "use-1"))
        assert read_session_binding(sink, S1)["recovered_from"] == "rematerialized"
        assert not _warnings(caplog)

    def test_12d_prime_same_bytes_different_binding_is_visible(self, env: Env, caplog):
        """Live provenance: identical policy content reached through a
        different v0.3.2 binding is a re-resolve, never silent."""
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        sidecar_path_for(sink).unlink()
        env.write_resolution(("claude-code-sess-*", "profiles/A.yaml"))  # same bytes, new pattern
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _run(env, _post(S1, "use-1"))
        entry = read_session_binding(sink, S1)
        assert entry["recovered_from"] == "reresolve"
        assert entry["binding"] == "claude-code-sess-*"
        assert set(_fingerprints(_events(sink))) == {fa}  # bytes unchanged
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert "claude-code-sess-*" in warnings[0] and "claude-code-*" in warnings[0]

    def test_12d_double_prime_legacy_registration_stays_fingerprint_only(self, env: Env, caplog):
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        _strip_provenance(sink, S1)
        assert not read_registration(sink, S1).has_provenance
        sidecar_path_for(sink).unlink()
        env.write_resolution(("claude-code-sess-*", "profiles/A.yaml"))
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _run(env, _post(S1, "use-1"))
        entry = read_session_binding(sink, S1)
        assert entry["recovered_from"] == "rematerialized"
        assert entry["binding"] == "claude-code-sess-*"
        assert not _warnings(caplog)

    def test_provenance_resolution_mismatch_with_equal_fingerprint_is_visible(
        self, env: Env, caplog
    ):
        """Registered degraded onto the default; the binding target then
        appears with the default's content. Same bytes, resolution differs."""
        env.default.write_text(PROFILE_D, encoding="utf-8")
        env.write_resolution(("claude-code-*", "profiles/missing.yaml"))
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        assert read_registration(sink, S1).profile_resolution == "degraded"
        sidecar_path_for(sink).unlink()
        env.write_profile("missing.yaml", PROFILE_D)
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _run(env, _post(S1, "use-1"))
        entry = read_session_binding(sink, S1)
        assert entry["resolution"] == "bound" and entry["recovered_from"] == "reresolve"
        # One hook warning; the resolver's own "degraded" warning from the
        # first process is expected and separate.
        hook_warnings = [w for w in _warnings(caplog) if "re-resolved" in w]
        assert len(hook_warnings) == 1
        assert "resolution=degraded" in hook_warnings[0] and "resolution=bound" in hook_warnings[0]

    def test_reresolve_is_sticky_not_a_loop(self, env: Env, caplog):
        fa = env.fingerprint_of("A.yaml")
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        sidecar_path_for(sink).unlink()
        env.write_profile("A.yaml", PROFILE_B)
        fa_prime = env.fingerprint_of("A.yaml")
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _run(env, _post(S1, "use-1"))
            first = read_session_binding(sink, S1)
            for i in range(2, 6):
                _run(env, _pre(S1, f"use-{i}"))
                _run(env, _post(S1, f"use-{i}"))
        later = read_session_binding(sink, S1)
        assert first["recovered_from"] == "reresolve"
        assert later == first  # not rewritten: same bound_at, same everything
        assert len(_warnings(caplog)) == 1
        events = _events(sink)
        assert _registrations(events)[0]["profile_fingerprint"] == fa
        assert set(_fingerprints(events)[1:]) == {fa, fa_prime}
        assert all(fp == fa_prime for fp in _fingerprints(events)[-8:])
        # The disagreement stays observable to the diagnostic CLI.
        from sentience_governor.cli import ux as ux_mod

        report = ux_mod._inspect_session_binding(S1)
        assert report["status"] == "DISAGREES_WITH_REGISTRATION"
        assert report["recovered_from"] == "reresolve"
        assert report["registration_agreement"] == "disagrees: fingerprint"
