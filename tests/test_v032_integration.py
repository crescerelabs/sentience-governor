"""v0.3.2 CP6: integration of per-session policy binding (WS-A) with semantic
shell classification and per-effect policy matching (WS-B).

Every scenario runs through real Claude Code hook processes (one
``ClaudeCodeGovernanceHook`` per event against one trace directory), with
``resolution.yaml`` and profile files on disk, so what is proven is the
runtime path an operator gets, not the evaluator in isolation.

Rows of the locked test matrix covered here: 4 (two concurrent sessions,
different profiles), 10 (concurrent sticky sessions after configuration
changes), 35 (cross-process pre/post agreement and untouched adapter
surfaces), 43 (profile-dependent semantic governance), 44 (no synthetic
policy match through the bound-profile path), 45 (`profile snapshots`
against the integrated state), plus the release-wide invariants. Row 46
(companion suite against a locally widened pin) is a later checkpoint and
is deliberately not exercised.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import socket
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.cli import ux as ux_mod
from sentience_governor.profile import GovernanceProfile
from sentience_governor.schema.events import AdvisoryFlag, EventType
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.session_manager.resumption import read_session_binding, sidecar_path_for
from sentience_governor.sink.writer import SinkWriter
from sentience_governor.wrapper.shell_classification import classify_shell_command
from tests.test_claude_code_sticky_binding import (  # accepted CP2 harness
    Env,
    _CapturingSink,
    _Client,
    _events,
    _run,
    _warnings,
    env,  # noqa: F401  (fixture re-export)
)


def _pre(session: str, use_id: str = "use-1", *, command: str = "", tool: str = "Bash", **tool_input) -> dict:
    inp = dict(tool_input) if tool != "Bash" else {"command": command}
    return {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": tool,
            "tool_input": inp, "tool_use_id": use_id, "cwd": "/tmp"}


def _post(session: str, use_id: str = "use-1", *, command: str = "", tool: str = "Bash", **tool_input) -> dict:
    inp = dict(tool_input) if tool != "Bash" else {"command": command}
    return {"hook_event_name": "PostToolUse", "session_id": session, "tool_name": tool,
            "tool_input": inp, "tool_response": {"ok": True}, "tool_use_id": use_id, "cwd": "/tmp"}

HEX64 = re.compile(r"[0-9a-f]{64}")
HEX12 = re.compile(r"^[0-9a-f]{12}$")
FLAG = AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED.value

# Session ids whose first eight characters differ, so the derived agent ids
# (`claude-code-<first 8>`) resolve through different bindings.
SA = "alpha-session-0001-aaaa"   # agent claude-code-alpha-se
SB = "beta-session-0002-bbbb"    # agent claude-code-beta-ses

PROFILE_A = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  required: true\n"
    "  demand_at: never\n"
    "high_consequence:\n"
    "  operations:\n"
    "    - domain: cloud_infrastructure\n"
    "      destructive: true\n"
)
PROFILE_B = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  required: true\n"
    "  demand_at: session_start\n"
    "task_boundary:\n"
    "  signals: [time_gap]\n"
    "  time_gap_seconds: 120\n"
)
PROFILE_C = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  demand_at: first_write\n"
    "high_consequence:\n"
    "  tools:\n"
    "    - \"Bash:shell$\"\n"
)
PROFILE_NET = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  required: true\n"
    "  demand_at: never\n"
    "high_consequence:\n"
    "  operations:\n"
    "    - domain: network\n"
    "      action: modify\n"
)

TERMINATE = "aws ec2 terminate-instances --instance-ids i-0d64085d9f13c793c"
DESCRIBE = "aws ec2 describe-instances"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bind_two(env: Env) -> Tuple[str, str]:
    """Bindings: alpha → A, beta → B, catch-all → C (first match must win)."""
    env.write_profile("A.yaml", PROFILE_A)
    env.write_profile("B.yaml", PROFILE_B)
    env.write_profile("C.yaml", PROFILE_C)
    env.write_resolution(
        ("claude-code-alpha-*", "profiles/A.yaml"),
        ("claude-code-beta-*", "profiles/B.yaml"),
        ("claude-code-*", "profiles/C.yaml"),
    )
    return env.fingerprint_of("A.yaml"), env.fingerprint_of("B.yaml")


def _scopes(sink: Path) -> List[dict]:
    return [e for e in _events(sink) if e["event_type"] == EventType.SCOPE_ASSERTED.value]


def _registration(sink: Path) -> dict:
    regs = [e for e in _events(sink) if e["event_type"] == EventType.AGENT_REGISTERED.value]
    assert len(regs) == 1, "exactly one AGENT_REGISTERED"
    return regs[0]


def _bash(env: Env, session: str, use_id: str, command: str) -> dict:
    _run(env, _pre(session, use_id=use_id, command=command))
    _run(env, _post(session, use_id=use_id, command=command))
    return _scopes(env.sink_for(session))[-1]


def _assert_chain(sink: Path) -> None:
    events = _events(sink)
    assert [e["event_sequence_number"] for e in events] == list(range(1, len(events) + 1))
    for prev, cur in zip(events, events[1:]):
        assert cur["previous_event_id"] == prev["event_id"]
    for e in events:
        assert e["pass_through"] is True
        assert "event_id" in e and len(e["event_id"]) == 36


def _assert_public_evidence(sink: Path, fingerprint: str) -> None:
    text = sink.read_text(encoding="utf-8")
    assert not HEX64.search(text), "a full content hash leaked into the public trace"
    events = _events(sink)
    assert all(e.get("profile_fingerprint") == fingerprint for e in events)
    assert HEX12.match(fingerprint)
    for e in events:
        if e["event_type"] == EventType.SCOPE_ASSERTED.value and e["payload"]["tool_id"] == "Bash":
            assert e["payload"]["operation_type"] == "EXECUTE"
            assert "operation_classification" in e["payload"]


def _tree_signature(root: Path) -> Dict[str, Tuple[int, int, str]]:
    out: Dict[str, Tuple[int, int, str]] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        else:
            out[str(p.relative_to(root)) + "/"] = (0, 0, "dir")
    return out


# ---------------------------------------------------------------------------
# row 4: two concurrent sessions, different profiles
# ---------------------------------------------------------------------------


class TestRow4TwoSessionsDifferentProfiles:
    def test_first_match_binding_and_isolation(self, env: Env, caplog):
        fa, fb = _bind_two(env)
        fc = env.fingerprint_of("C.yaml")
        assert len({fa, fb, fc}) == 3
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            # interleave the two sessions
            _run(env, _pre(SA, "u1", command="git status"))
            _run(env, _pre(SB, "u1", command="git status"))
            _run(env, _post(SA, "u1", command="git status"))
            _run(env, _post(SB, "u1", command="git status"))
            _bash(env, SA, "u2", TERMINATE)
            _bash(env, SB, "u2", TERMINATE)
            _bash(env, SA, "u3", "rm -rf /tmp/x && aws ec2 describe-instances")
            _bash(env, SB, "u3", "rm -rf /tmp/x && aws ec2 describe-instances")
        assert not _warnings(caplog)

        for session, fp, pattern in ((SA, fa, "claude-code-alpha-*"), (SB, fb, "claude-code-beta-*")):
            sink = env.sink_for(session)
            reg = _registration(sink)
            assert reg["payload"]["profile_resolution"] == "bound"
            assert reg["payload"]["profile_binding"] == pattern
            assert reg["profile_fingerprint"] == fp
            entry = read_session_binding(sink, session)
            assert entry["binding"] == pattern and entry["profile_fingerprint"] == fp
            assert entry["recovered_from"] is None
            _assert_chain(sink)
            _assert_public_evidence(sink, fp)

        # Policy differs per session on identical commands (demand_at never vs session_start).
        a_scope = _scopes(env.sink_for(SA))[0]
        b_scope = _scopes(env.sink_for(SB))[0]
        assert "POL-001" not in a_scope["policy_violations"]
        assert "POL-001" in b_scope["policy_violations"]
        # Classification is identical regardless of which profile is bound.
        for i in range(3):
            assert (_scopes(env.sink_for(SA))[i]["payload"]["operation_classification"]
                    == _scopes(env.sink_for(SB))[i]["payload"]["operation_classification"])
            assert (_scopes(env.sink_for(SA))[i]["payload"]["target_system"]
                    == _scopes(env.sink_for(SB))[i]["payload"]["target_system"])
        # No cross-session leakage of fingerprints anywhere in either trace.
        assert fb not in env.sink_for(SA).read_text(encoding="utf-8")
        assert fa not in env.sink_for(SB).read_text(encoding="utf-8")
        assert fc not in env.sink_for(SA).read_text(encoding="utf-8") + env.sink_for(SB).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# row 10: concurrent sticky sessions after configuration changes
# ---------------------------------------------------------------------------


class TestRow10StickyAndClassificationCoexist:
    def test_configuration_changes_move_neither_session(self, env: Env, caplog):
        fa, fb = _bind_two(env)
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            _bash(env, SA, "u1", "git status")
            _bash(env, SB, "u1", "git status")
            entry_a = read_session_binding(env.sink_for(SA), SA)
            entry_b = read_session_binding(env.sink_for(SB), SB)

            # Swap the mapping AND change both profile files on disk:
            # A loses its cloud rule, B gains it.
            env.write_resolution(
                ("claude-code-alpha-*", "profiles/B.yaml"),
                ("claude-code-beta-*", "profiles/A.yaml"),
            )
            env.write_profile("A.yaml", PROFILE_B)
            env.write_profile("B.yaml", PROFILE_A)
            assert env.fingerprint_of("A.yaml") != fa and env.fingerprint_of("B.yaml") != fb

            term_a = _bash(env, SA, "u2", TERMINATE)
            term_b = _bash(env, SB, "u2", TERMINATE)
            desc_a = _bash(env, SA, "u3", DESCRIBE)
            desc_b = _bash(env, SB, "u3", DESCRIBE)
            curl_a = _bash(env, SA, "u4", "curl -o page.html https://example.com")
            curl_b = _bash(env, SB, "u4", "curl -o page.html https://example.com")
        assert not _warnings(caplog)  # no recovery, no warning in normal sticky operation

        # Bindings untouched, byte for byte.
        assert read_session_binding(env.sink_for(SA), SA) == entry_a
        assert read_session_binding(env.sink_for(SB), SB) == entry_b
        assert entry_a["recovered_from"] is None and entry_b["recovered_from"] is None

        # Policy evaluation uses each session's sticky profile, not the disk:
        # A (original rule) flags terminate; B (original, no rule) does not.
        assert FLAG in term_a["advisory_flags"]
        assert FLAG not in term_b["advisory_flags"]
        assert FLAG not in desc_a["advisory_flags"] and FLAG not in desc_b["advisory_flags"]
        assert FLAG not in curl_a["advisory_flags"] and FLAG not in curl_b["advisory_flags"]
        # Original demand_at semantics persist too.
        assert "POL-001" not in term_a["policy_violations"]
        assert "POL-001" in term_b["policy_violations"]

        # Classifications follow the actual commands, identically in both sessions.
        for x, y, domain, action in ((term_a, term_b, "cloud_infrastructure", "delete"),
                                     (desc_a, desc_b, "cloud_infrastructure", "read"),
                                     (curl_a, curl_b, "network", "read")):
            oc = x["payload"]["operation_classification"]
            assert oc == y["payload"]["operation_classification"]
            assert oc["segments"][0]["effects"][0]["domain"] == domain
            assert oc["segments"][0]["effects"][0]["action"] == action
        assert term_a["payload"]["target_system"] == "shell/cloud_infrastructure"
        assert curl_a["payload"]["target_system"] == "shell"

        for session, fp, pattern in ((SA, fa, "claude-code-alpha-*"), (SB, fb, "claude-code-beta-*")):
            sink = env.sink_for(session)
            reg = _registration(sink)
            assert reg["profile_fingerprint"] == fp and reg["payload"]["profile_binding"] == pattern
            _assert_chain(sink)
            _assert_public_evidence(sink, fp)

        # A NEW alpha session follows the changed disk configuration: the
        # swapped mapping sends it to B.yaml, whose content is now PROFILE_A.
        new_session = "alpha-session-0009-zzzz"
        _run(env, _pre(new_session, "u1", command="git status"))
        new_reg = _registration(env.sink_for(new_session))
        assert new_reg["payload"]["profile_binding"] == "claude-code-alpha-*"
        assert new_reg["profile_fingerprint"] == env.fingerprint_of("B.yaml") == fa
        assert new_reg["profile_fingerprint"] != fb


# ---------------------------------------------------------------------------
# row 35: cross-process pre/post agreement and untouched surfaces
# ---------------------------------------------------------------------------


class TestRow35CrossProcessAndUntouchedSurfaces:
    def test_pre_post_agree_and_non_bash_omit(self, env: Env):
        _bind_two(env)
        for cmd in ("git status", "cat a > b", "curl -o x https://x", "./deploy.sh", "ls x 2>/dev/null"):
            uid = f"u-{abs(hash(cmd)) % 10000}"
            _run(env, _pre(SA, uid, command=cmd))
            _run(env, _post(SA, uid, command=cmd))
        events = _events(env.sink_for(SA))
        scopes = [e for e in events if e["event_type"] == EventType.SCOPE_ASSERTED.value]
        snaps = [e for e in events if e["event_type"] == EventType.CONTEXT_SNAPSHOT.value]
        assert len(scopes) == 5 and len(snaps) == 10
        for i, scope in enumerate(scopes):
            target = scope["payload"]["target_system"]
            assert snaps[2 * i]["payload"]["provenance"] == [target]      # pre-call
            assert snaps[2 * i + 1]["payload"]["provenance"] == [target]  # post-call
            assert "operation_classification" in scope["payload"]
        assert [s["payload"]["target_system"] for s in scopes] == [
            "shell/version_control", "shell/filesystem", "shell", "shell", "shell/filesystem"]

        # Non-Bash and mcp__* hook tools: no object, legacy targets and op types.
        for tool, kwargs, target, op in [("Edit", {"file_path": "/x/a.py"}, "filesystem", "WRITE"),
                                         ("Read", {"file_path": "/x/a.py"}, "filesystem", "READ"),
                                         ("mcp__github__list_issues", {"repo": "x"}, "github", "READ"),
                                         ("mcp__db__write_row", {"row": 1}, "db", "WRITE")]:
            _run(env, _pre(SA, f"u-{tool}", tool=tool, **kwargs))
            scope = _scopes(env.sink_for(SA))[-1]
            assert scope["payload"]["tool_id"] == tool
            assert "operation_classification" not in scope["payload"], tool
            assert scope["payload"]["target_system"] == target and scope["payload"]["operation_type"] == op, tool
        _assert_chain(env.sink_for(SA))

    @pytest.mark.asyncio
    async def test_mcp_wrapper_unchanged(self, env: Env):
        from sentience_governor.wrapper.mcp import SentienceMCPAdapter, wrap_mcp_client

        env.write_profile("A.yaml", PROFILE_A)
        env.write_resolution(("mcp-agent-*", "profiles/A.yaml"))
        captured = _CapturingSink()
        wrapped = wrap_mcp_client(
            target=SentienceMCPAdapter(delegate=_Client(), call_fn=lambda c, n, a: c.invoke(n, a)),
            session_manager=SessionManager(), cache=InProcessCache(), sink_writer=SinkWriter(captured),
            agent_id="mcp-agent-1", declared_capabilities=["crm.read"], session_id="sess-mcp-1",
        )
        async with wrapped:
            wrapped.send_tool_call("Bash", {"command": "aws ec2 terminate-instances --instance-ids i-1"})
            wrapped.send_tool_call("crm.fetch", {"id": "1"})
        dumped = [e.to_dict() for e in captured.events]
        scopes = [e for e in dumped if e["event_type"] == EventType.SCOPE_ASSERTED.value]
        assert len(scopes) == 2
        for s in scopes:
            assert "operation_classification" not in s["payload"]
            assert FLAG not in s["advisory_flags"]  # operations rules need a classification; MCP passes none
        assert dumped[0]["payload"]["profile_resolution"] == "bound"
        assert dumped[0]["profile_fingerprint"] == env.fingerprint_of("A.yaml")
        assert not HEX64.search(json.dumps(dumped))

    def test_langchain_unchanged(self, env: Env):
        from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler

        env.write_profile("A.yaml", PROFILE_A)
        env.write_resolution(("lc-agent", "profiles/A.yaml"))
        captured = _CapturingSink()
        handler = SentienceCallbackHandler(agent_id="lc-agent", session_manager=SessionManager(), cache=InProcessCache(),
                                           sink_writer=SinkWriter(captured), declared_capabilities=["test.read"])
        handler.on_chain_start({}, {"input": "go"})
        handler.on_tool_start({"name": "Bash"}, "aws ec2 terminate-instances --instance-ids i-1")
        dumped = [e.to_dict() for e in captured.events]
        scopes = [e for e in dumped if e["event_type"] == EventType.SCOPE_ASSERTED.value]
        assert scopes and all("operation_classification" not in s["payload"] for s in scopes)
        assert all(FLAG not in s["advisory_flags"] for s in scopes)
        assert dumped[0]["payload"]["profile_resolution"] == "bound"
        assert not HEX64.search(json.dumps(dumped))


# ---------------------------------------------------------------------------
# rows 43 and 44: profile-dependent semantic governance through the hook
# ---------------------------------------------------------------------------


class TestRow43ProfileDependentGovernance:
    def test_terminate_flags_only_under_the_profile_with_the_rule(self, env: Env):
        fa, fb = _bind_two(env)
        term_a = _bash(env, SA, "u1", TERMINATE)
        term_b = _bash(env, SB, "u1", TERMINATE)
        desc_a = _bash(env, SA, "u2", DESCRIBE)
        desc_b = _bash(env, SB, "u2", DESCRIBE)
        assert FLAG in term_a["advisory_flags"]
        assert FLAG not in term_b["advisory_flags"]
        assert FLAG not in desc_a["advisory_flags"]
        assert FLAG not in desc_b["advisory_flags"]
        for scope in (term_a, term_b):
            assert scope["payload"]["operation_classification"]["segments"] == [{
                "executable": "aws", "subcommand": "ec2 terminate-instances",
                "effects": [{"domain": "cloud_infrastructure", "action": "delete", "destructive": True}]}]
            assert scope["payload"]["target_system"] == "shell/cloud_infrastructure"
            assert scope["payload"]["operation_type"] == "EXECUTE"
        for scope in (desc_a, desc_b):
            assert scope["payload"]["operation_classification"]["segments"][0]["effects"] == [
                {"domain": "cloud_infrastructure", "action": "read", "destructive": False}]
        assert term_a["profile_fingerprint"] == fa and desc_a["profile_fingerprint"] == fa
        assert term_b["profile_fingerprint"] == fb and desc_b["profile_fingerprint"] == fb
        # Non-blocking default: the flag is advisory, the call passes through.
        assert term_a["pass_through"] is True
        assert term_a["simulated_consequence"] is None  # demand_at never in A; no policy violation
        for session, fp in ((SA, fa), (SB, fb)):
            _assert_chain(env.sink_for(session))
            _assert_public_evidence(env.sink_for(session), fp)


class TestRow44NoSyntheticPolicyMatch:
    def test_curl_o_under_network_modify_rule_does_not_match(self, env: Env):
        env.write_profile("N.yaml", PROFILE_NET)
        env.write_resolution(("claude-code-*", "profiles/N.yaml"))
        fn = env.fingerprint_of("N.yaml")
        scope = _bash(env, SA, "u1", "curl -o x https://example.com/file")
        assert scope["profile_fingerprint"] == fn
        assert scope["payload"]["operation_classification"]["segments"] == [{"executable": "curl", "effects": [
            {"domain": "network", "action": "read", "destructive": False},
            {"domain": "filesystem", "action": "modify", "destructive": None}]}]
        assert FLAG not in scope["advisory_flags"]
        # And the same rule DOES match a real network write, through the same path.
        post = _bash(env, SA, "u2", "curl -X POST -d '{}' https://example.com/items")
        assert FLAG in post["advisory_flags"]
        assert post["payload"]["operation_classification"]["segments"][0]["effects"] == [
            {"domain": "network", "action": "modify", "destructive": None}]
        # Registration provenance and public evidence intact.
        reg = _registration(env.sink_for(SA))
        assert reg["payload"]["profile_resolution"] == "bound" and reg["payload"]["profile_binding"] == "claude-code-*"
        _assert_public_evidence(env.sink_for(SA), fn)


# ---------------------------------------------------------------------------
# row 45: profile snapshots against the integrated state
# ---------------------------------------------------------------------------


class TestRow45SnapshotsAgainstIntegratedState:
    def test_snapshots_listing_is_correct_and_read_only(self, env: Env, capsys):
        fa, fb = _bind_two(env)
        _bash(env, SA, "u1", "git status")
        _bash(env, SB, "u1", "git status")
        # A third session with A's content through a different binding pattern
        # (identical bytes → same content-addressed snapshot).
        env.write_profile("A2.yaml", PROFILE_A)
        env.write_resolution(("claude-code-delta-*", "profiles/A2.yaml"),
                             ("claude-code-alpha-*", "profiles/A.yaml"),
                             ("claude-code-beta-*", "profiles/B.yaml"))
        _bash(env, "delta-session-0004", "u1", "git status")

        before = (_tree_signature(env.sink_base), _tree_signature(env.home))
        code = ux_mod.run_profile_snapshots(argparse.Namespace(json=True))
        rows = json.loads(capsys.readouterr().out)
        after = (_tree_signature(env.sink_base), _tree_signature(env.home))
        assert code == 0
        assert before == after, "profile snapshots mutated something"

        pa = GovernanceProfile.from_file(env.profiles / "A.yaml")
        pb = GovernanceProfile.from_file(env.profiles / "B.yaml")
        by_hash = {r["content_hash"]: r for r in rows}
        assert set(by_hash) == {pa.content_hash(), pb.content_hash()}  # shared content → one file
        assert by_hash[pa.content_hash()]["fingerprint"] == fa == pa.content_hash()[:12]
        assert by_hash[pb.content_hash()]["fingerprint"] == fb
        assert by_hash[pa.content_hash()]["sessions"] == 2   # alpha + delta
        assert by_hash[pb.content_hash()]["sessions"] == 1
        assert all(r["verified"] is True for r in rows)
        assert all(HEX64.fullmatch(r["content_hash"]) for r in rows)
        assert all(r["bytes"] == len(p.canonical_bytes()) for r, p in ((by_hash[pa.content_hash()], pa), (by_hash[pb.content_hash()], pb)))
        # Human listing, also read-only.
        code = ux_mod.run_profile_snapshots(argparse.Namespace(json=False))
        out = capsys.readouterr().out
        assert code == 0 and pa.content_hash() in out and "yes" in out
        assert (_tree_signature(env.sink_base), _tree_signature(env.home)) == before
        # The 64-hex hash lives only in the diagnostic surface, never in the traces.
        for session in (SA, SB, "delta-session-0004"):
            assert not HEX64.search(env.sink_for(session).read_text(encoding="utf-8"))

    def test_resolve_session_id_ok_for_both_sessions(self, env: Env, capsys):
        fa, fb = _bind_two(env)
        _bash(env, SA, "u1", "git status")
        _bash(env, SB, "u1", "git status")
        for session, fp, pattern in ((SA, fa, "claude-code-alpha-*"), (SB, fb, "claude-code-beta-*")):
            code = ux_mod.run_profile_resolve(argparse.Namespace(agent_id=None, session_id=session, json=True))
            report = json.loads(capsys.readouterr().out)
            assert code == 0 and report["status"] == "OK"
            assert report["fingerprint"] == fp and report["binding"] == pattern
            assert report["registration_agreement"] == "agrees" and report["recovered_from"] is None


# ---------------------------------------------------------------------------
# release-wide invariants across the integrated scenarios
# ---------------------------------------------------------------------------


class TestReleaseInvariants:
    def test_binding_and_classification_are_independent(self, env: Env):
        fa, fb = _bind_two(env)
        commands = ["git status", "rm -rf /tmp/x && aws ec2 describe-instances", TERMINATE, "echo $(x)", "cd /tmp"]
        for i, cmd in enumerate(commands):
            _bash(env, SA, f"u{i}", cmd)
            _bash(env, SB, f"u{i}", cmd)
        entry_a = read_session_binding(env.sink_for(SA), SA)
        entry_b = read_session_binding(env.sink_for(SB), SB)
        sa, sb = _scopes(env.sink_for(SA)), _scopes(env.sink_for(SB))
        for i, cmd in enumerate(commands):
            # policy binding never alters classifier output for the same command...
            assert sa[i]["payload"]["operation_classification"] == sb[i]["payload"]["operation_classification"]
            assert sa[i]["payload"]["operation_classification"] == classify_shell_command(cmd).model_dump()
            # ...and classification never alters the binding identity.
            assert sa[i]["profile_fingerprint"] == fa and sb[i]["profile_fingerprint"] == fb
        assert read_session_binding(env.sink_for(SA), SA) == entry_a
        assert read_session_binding(env.sink_for(SB), SB) == entry_b
        assert entry_a["recovered_from"] is None and entry_b["recovered_from"] is None

    def test_no_egress_during_hook_processing(self, env: Env, monkeypatch):
        _bind_two(env)

        def deny(*a, **k):
            raise AssertionError("network egress attempted by the hook")

        monkeypatch.setattr(socket, "socket", deny)
        monkeypatch.setattr(socket, "create_connection", deny)
        _bash(env, SA, "u1", TERMINATE)
        _bash(env, SB, "u1", "curl -o x https://example.com")
        assert FLAG in _scopes(env.sink_for(SA))[-1]["advisory_flags"]

    def test_sidecar_carries_hashes_but_traces_do_not(self, env: Env):
        fa, fb = _bind_two(env)
        _bash(env, SA, "u1", "git status")
        sidecar = sidecar_path_for(env.sink_for(SA)).read_text(encoding="utf-8")
        assert HEX64.search(sidecar)  # storage identity lives in the sidecar
        assert not HEX64.search(env.sink_for(SA).read_text(encoding="utf-8"))
        assert HEX12.match(fa)
