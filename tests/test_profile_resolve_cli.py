"""v0.3.2 CP3: read-only profile inspection CLI.

* ``sentience profile resolve --agent-id ID``: what a new session would
  resolve to; exit 0 always; writes nothing.
* ``sentience profile resolve --session-id SID``: inspects the sticky
  binding and snapshot of an existing Claude Code session and reports one
  of OK, NO_BINDING, SNAPSHOT_MISSING, SNAPSHOT_CORRUPTED, BINDING_INVALID,
  DISAGREES_WITH_REGISTRATION, NO_TRACE. Exit 0 for OK and NO_BINDING,
  exit 1 for every other status. Registration disagreement covers the
  fingerprint and, when the registration recorded provenance, the
  resolution and binding pattern.
* ``sentience profile snapshots``: lists snapshot files with
  verification and referencing-session counts; exit 0 always.
* ``sentience profile validate [PATH]``: retained read-only validation.

Every command is verified read-only by fingerprinting the whole trace
directory tree (paths, sizes, mtimes, bytes) before and after the call.

Handlers are called directly with an ``argparse.Namespace`` (the
existing profile CLI test style); the argparse contract is exercised
through ``main()`` with the first-run flow disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, Tuple

import pytest

from sentience_governor.cli import ux as ux_mod
from sentience_governor.profile import GovernanceProfile
from sentience_governor.session_manager.resumption import (
    _SESSION_BINDING_KEY,
    build_binding_entry,
    read_session_binding,
    record_session_binding,
    sidecar_path_for,
)
from sentience_governor.wrapper import claude_code_hook as cch
from tests.test_claude_code_sticky_binding import (
    PROFILE_A,
    PROFILE_B,
    PROFILE_D,
    S1,
    S2,
    Env,
    _events,
    _post,
    _pre,
    _run,
    env,  # noqa: F401  (fixture re-export)
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ns(**kwargs) -> argparse.Namespace:
    base = {"agent_id": None, "session_id": None, "json": False, "path": None, "strict": False}
    base.update(kwargs)
    return argparse.Namespace(**base)


def _tree_signature(root: Path) -> Dict[str, Tuple[int, int, str]]:
    """Every file under ``root`` with size, mtime_ns and content digest.
    Absent root → empty signature (so 'still absent' also compares equal)."""
    out: Dict[str, Tuple[int, int, str]] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (
                st.st_size,
                st.st_mtime_ns,
                hashlib.sha256(p.read_bytes()).hexdigest(),
            )
        else:
            out[str(p.relative_to(root)) + "/"] = (0, 0, "dir")
    return out


def _read_only(env: Env, fn):
    """Run ``fn()`` and assert the trace tree, the home tree and the
    fallback directory are byte-identical afterwards."""
    fallback = cch._FALLBACK_SINK_DIR
    before = (_tree_signature(env.sink_base), _tree_signature(env.home), _tree_signature(fallback))
    result = fn()
    after = (_tree_signature(env.sink_base), _tree_signature(env.home), _tree_signature(fallback))
    assert before == after, "CLI command wrote something"
    return result


def _resolve_agent(env: Env, agent_id: str, capsys, *, as_json=False):
    code = _read_only(env, lambda: ux_mod.run_profile_resolve(_ns(agent_id=agent_id, json=as_json)))
    out = capsys.readouterr().out
    return code, (json.loads(out) if as_json else out)


def _resolve_session(env: Env, session_id: str, capsys, *, as_json=False):
    code = _read_only(
        env, lambda: ux_mod.run_profile_resolve(_ns(session_id=session_id, json=as_json))
    )
    out = capsys.readouterr().out
    return code, (json.loads(out) if as_json else out)


def _snapshots(env: Env, capsys, *, as_json=False):
    code = _read_only(env, lambda: ux_mod.run_profile_snapshots(_ns(json=as_json)))
    out = capsys.readouterr().out
    return code, (json.loads(out) if as_json else out)


# ---------------------------------------------------------------------------
# resolve --agent-id
# ---------------------------------------------------------------------------


class TestResolveAgent:
    def test_bound(self, env: Env, capsys):
        code, report = _resolve_agent(env, "claude-code-1234", capsys, as_json=True)
        assert code == 0
        prof = GovernanceProfile.from_file(env.profiles / "A.yaml")
        assert report == {
            "agent_id": "claude-code-1234",
            "resolution": "bound",
            "binding": "claude-code-*",
            "source_path": str(env.profiles / "A.yaml"),
            "fingerprint": prof.fingerprint(),
            "content_hash": prof.content_hash(),
            "warnings": [],
        }

    def test_degraded_with_warning(self, env: Env, capsys):
        env.default.write_text(PROFILE_D, encoding="utf-8")
        env.write_resolution(("claude-code-*", "profiles/missing.yaml"))
        code, report = _resolve_agent(env, "claude-code-1234", capsys, as_json=True)
        assert code == 0
        assert report["resolution"] == "degraded" and report["binding"] == "claude-code-*"
        assert report["fingerprint"] == GovernanceProfile.from_file(env.default).fingerprint()
        assert report["warnings"] and "missing.yaml" in report["warnings"][0]

    def test_default_and_none(self, env: Env, capsys):
        code, report = _resolve_agent(env, "other-agent", capsys, as_json=True)
        assert code == 0 and report["resolution"] == "none"
        assert report["binding"] is None and report["fingerprint"] is None
        env.default.write_text(PROFILE_D, encoding="utf-8")
        code, report = _resolve_agent(env, "other-agent", capsys, as_json=True)
        assert code == 0 and report["resolution"] == "default" and report["binding"] is None
        assert report["source_path"] == str(env.default)

    def test_human_output_lists_every_field(self, env: Env, capsys):
        code, out = _resolve_agent(env, "claude-code-1234", capsys)
        assert code == 0
        for key in ("agent_id:", "resolution:", "binding:", "source_path:", "fingerprint:",
                    "content_hash:", "warnings:"):
            assert key in out
        assert "bound" in out and "claude-code-*" in out and "(none)" in out  # warnings

    def test_creates_no_snapshot_binding_or_sink(self, env: Env, capsys):
        assert not env.sink_base.exists()
        _resolve_agent(env, "claude-code-1234", capsys)
        assert not env.sink_base.exists()
        assert not (env.home / "profiles" / "snapshots").exists()
        assert sorted(p.name for p in env.profiles.iterdir()) == ["A.yaml", "B.yaml"]


# ---------------------------------------------------------------------------
# resolve --session-id: every status and the exit-code contract
# ---------------------------------------------------------------------------


class TestResolveSession:
    def test_ok(self, env: Env, capsys):
        _run(env, _pre(S1, "use-1"))
        _run(env, _post(S1, "use-1"))
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 0
        assert report["status"] == "OK"
        assert report["trace"] == str(env.sink_for(S1))
        assert report["trace_last_append"].endswith("UTC")
        assert report["resolution"] == "bound" and report["binding"] == "claude-code-*"
        assert report["source_path"] == str(env.profiles / "A.yaml")
        assert report["fingerprint"] == env.fingerprint_of("A.yaml")
        assert report["fingerprint"] == report["content_hash"][:12]
        assert report["snapshot"] == f"profiles/{report['content_hash']}.json"
        assert report["snapshot_verified"] is True
        assert report["registration_fingerprint"] == report["fingerprint"]
        assert report["registration_resolution"] == "bound"
        assert report["registration_binding"] == "claude-code-*"
        assert report["registration_agreement"] == "agrees"
        assert report["bound_at"].endswith("Z")
        assert report["bound_by"].startswith("claude_code_hook/")
        assert report["recovered_from"] is None

    def test_ok_human_output(self, env: Env, capsys):
        _run(env, _pre(S1))
        code, out = _resolve_session(env, S1, capsys)
        assert code == 0
        for key in ("session_id:", "trace:", "resolution:", "binding:", "source_path:",
                    "fingerprint:", "content_hash:", "snapshot:", "registration:",
                    "bound_at:", "recovered_from:", "status:"):
            assert key in out
        assert "verified" in out and "agrees" in out and "status:          OK" in out
        assert "by claude_code_hook/" in out

    def test_no_trace(self, env: Env, capsys):
        code, report = _resolve_session(env, "never-seen", capsys, as_json=True)
        assert code == 1 and report["status"] == "NO_TRACE"
        assert report["trace"] is None

    def test_no_binding_pre_032_session(self, env: Env, capsys):
        """A trace without a binding bucket (0.3.1.2 session): NO_BINDING, exit 0."""
        _run(env, _pre(S1))
        sink = env.sink_for(S1)
        index = json.loads(sidecar_path_for(sink).read_text())
        del index[_SESSION_BINDING_KEY]
        sidecar_path_for(sink).write_text(json.dumps(index))
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 0 and report["status"] == "NO_BINDING"
        assert report["registration_fingerprint"] == env.fingerprint_of("A.yaml")
        assert report["resolution"] is None

    def test_no_binding_when_sidecar_absent(self, env: Env, capsys):
        _run(env, _pre(S1))
        sidecar_path_for(env.sink_for(S1)).unlink()
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 0 and report["status"] == "NO_BINDING"

    def test_snapshot_missing(self, env: Env, capsys):
        _run(env, _pre(S1))
        sink = env.sink_for(S1)
        (sink.parent / read_session_binding(sink, S1)["snapshot"]).unlink()
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 1 and report["status"] == "SNAPSHOT_MISSING"
        assert report["snapshot_verified"] is False
        assert report["fingerprint"] == env.fingerprint_of("A.yaml")  # binding still shown

    def test_snapshot_corrupted_and_not_repaired(self, env: Env, capsys):
        _run(env, _pre(S1))
        sink = env.sink_for(S1)
        snap = sink.parent / read_session_binding(sink, S1)["snapshot"]
        snap.write_bytes(b"{corrupt")
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 1 and report["status"] == "SNAPSHOT_CORRUPTED"
        assert report["snapshot_verified"] is False
        assert snap.read_bytes() == b"{corrupt"  # diagnostic never repairs
        _, out = _resolve_session(env, S1, capsys)
        assert "NOT verified" in out

    def test_snapshot_valid_bytes_but_not_a_profile_is_corrupted(self, env: Env, capsys):
        """Bytes that hash correctly by construction cannot fail the
        profile self-check, so exercise the self-check seam directly."""
        _run(env, _pre(S1))
        sink = env.sink_for(S1)
        entry = read_session_binding(sink, S1)
        other = env.profiles / "B.yaml"
        prof_b = GovernanceProfile.from_file(other)
        # Point the binding at B's (verified) snapshot while claiming A's
        # fingerprint: validation fails on the prefix relation.
        h = prof_b.content_hash()
        (sink.parent / "profiles" / f"{h}.json").write_bytes(prof_b.canonical_bytes())
        record_session_binding(
            sink, S1, dict(entry, profile_content_hash=h, snapshot=f"profiles/{h}.json")
        )
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 1 and report["status"] == "BINDING_INVALID"

    def test_binding_invalid(self, env: Env, capsys):
        _run(env, _pre(S1))
        sink = env.sink_for(S1)
        entry = read_session_binding(sink, S1)
        record_session_binding(sink, S1, dict(entry, profile_fingerprint="0" * 12))
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 1 and report["status"] == "BINDING_INVALID"
        assert report["fingerprint"] is None  # nothing from the tampered entry is shown
        assert report["registration_fingerprint"] == env.fingerprint_of("A.yaml")

    def test_disagrees_on_fingerprint(self, env: Env, capsys):
        _run(env, _pre(S1))
        sink = env.sink_for(S1)
        entry = read_session_binding(sink, S1)
        prof_b = GovernanceProfile.from_file(env.profiles / "B.yaml")
        h = prof_b.content_hash()
        (sink.parent / "profiles" / f"{h}.json").write_bytes(prof_b.canonical_bytes())
        record_session_binding(
            sink,
            S1,
            dict(entry, profile_fingerprint=h[:12], profile_content_hash=h, snapshot=f"profiles/{h}.json"),
        )
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 1 and report["status"] == "DISAGREES_WITH_REGISTRATION"
        assert report["snapshot_verified"] is True
        assert report["registration_agreement"] == "disagrees: fingerprint"

    def test_disagrees_on_provenance_with_equal_fingerprint(self, env: Env, capsys):
        _run(env, _pre(S1))
        sink = env.sink_for(S1)
        entry = read_session_binding(sink, S1)
        record_session_binding(sink, S1, dict(entry, binding="claude-code-prod-*"))
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 1 and report["status"] == "DISAGREES_WITH_REGISTRATION"
        assert report["registration_agreement"] == "disagrees: profile_binding"
        record_session_binding(sink, S1, dict(entry, resolution="degraded", binding="other-*"))
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 1
        assert report["registration_agreement"] == "disagrees: profile_resolution, profile_binding"
        _, out = _resolve_session(env, S1, capsys)
        assert "disagrees: profile_resolution, profile_binding" in out

    def test_legacy_registration_compares_fingerprint_only(self, env: Env, capsys):
        _run(env, _pre(S1))
        sink = env.sink_for(S1)
        lines = sink.read_text(encoding="utf-8").splitlines()
        first = json.loads(lines[0])
        first["payload"].pop("profile_resolution")
        first["payload"].pop("profile_binding")
        lines[0] = json.dumps(first)
        sink.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
        entry = read_session_binding(sink, S1)
        record_session_binding(sink, S1, dict(entry, binding="claude-code-prod-*"))
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 0 and report["status"] == "OK"
        assert report["registration_resolution"] is None
        assert report["registration_agreement"] == "agrees"

    def test_reresolve_binding_reports_disagreement_while_runtime_stays_sticky(
        self, env: Env, capsys
    ):
        _run(env, _pre(S1, "use-1"))
        sink = env.sink_for(S1)
        sidecar_path_for(sink).unlink()
        env.write_profile("A.yaml", PROFILE_B)
        _run(env, _post(S1, "use-1"))
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 1 and report["status"] == "DISAGREES_WITH_REGISTRATION"
        assert report["recovered_from"] == "reresolve"
        assert report["fingerprint"] == env.fingerprint_of("A.yaml")
        assert report["registration_fingerprint"] != report["fingerprint"]
        # The diagnostic changed nothing: the runtime keeps the sticky binding.
        _run(env, _pre(S1, "use-2"))
        assert read_session_binding(sink, S1)["recovered_from"] == "reresolve"
        assert _events(sink)[-1]["profile_fingerprint"] == report["fingerprint"]

    def test_bound_to_no_profile_is_ok(self, env: Env, capsys):
        env.resolution.unlink()
        _run(env, _pre(S1))
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 0 and report["status"] == "OK"
        assert report["resolution"] == "none"
        assert report["fingerprint"] is None and report["snapshot"] is None
        assert report["snapshot_verified"] is None
        assert report["registration_agreement"] == "agrees"

    def test_binding_without_registration_is_ok(self, env: Env, capsys):
        """Crash between binding and registration (row 12a): the trace file
        exists (touched by the lock) but has no registration yet."""
        sink = env.sink_for(S1)
        with cch.sink_lock(sink):
            cch._establish_or_rehydrate_binding(sink, S1, cch._derive_agent_id(S1), None)
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 0 and report["status"] == "OK"
        assert report["registration_agreement"] == "(none)"
        assert report["registration_fingerprint"] is None

    def test_shared_file_mode_lookup(self, env: Env, capsys, monkeypatch):
        shared = env.sink_base / "shared.jsonl"
        env.sink_base = shared
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(shared))
        _run(env, _pre(S1), shared=True)
        _run(env, _pre(S2), shared=True)
        env.sink_base = shared.parent  # for the read-only tree check
        for sid in (S1, S2):
            code, report = _resolve_session(env, sid, capsys, as_json=True)
            assert code == 0 and report["status"] == "OK"
            assert report["trace"] == str(shared)

    def test_fallback_directory_lookup(self, env: Env, capsys):
        fallback = cch._FALLBACK_SINK_DIR
        fallback.mkdir()
        sink = fallback / f"{S1}.jsonl"
        cch.ClaudeCodeGovernanceHook(_pre(S1), sink).process()
        code, report = _resolve_session(env, S1, capsys, as_json=True)
        assert code == 0 and report["status"] == "OK"
        assert report["trace"] == str(sink)

    def test_every_status_maps_to_the_exit_contract(self):
        zero = {"OK", "NO_BINDING"}
        for status in ("OK", "NO_BINDING", "SNAPSHOT_MISSING", "SNAPSHOT_CORRUPTED",
                       "BINDING_INVALID", "DISAGREES_WITH_REGISTRATION", "NO_TRACE"):
            assert (status in ux_mod._RESOLVE_EXIT_ZERO) == (status in zero)


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    def test_lists_shared_snapshot_with_session_count(self, env: Env, capsys):
        _run(env, _pre(S1))
        _run(env, _pre(S2))
        code, rows = _snapshots(env, capsys, as_json=True)
        assert code == 0
        assert len(rows) == 1
        row = rows[0]
        prof = GovernanceProfile.from_file(env.profiles / "A.yaml")
        assert row["content_hash"] == prof.content_hash()
        assert row["fingerprint"] == prof.fingerprint()
        assert row["bytes"] == len(prof.canonical_bytes())
        assert row["verified"] is True
        assert row["sessions"] == 2
        assert row["directory"] == str(env.sink_base)

    def test_corrupt_file_is_shown_not_fixed_exit_zero(self, env: Env, capsys):
        _run(env, _pre(S1))
        env.write_resolution(("claude-code-*", "profiles/B.yaml"))
        _run(env, _pre(S2))
        profiles_dir = env.sink_base / "profiles"
        target = sorted(profiles_dir.glob("*.json"))[0]
        target.write_bytes(b"xx")
        code, rows = _snapshots(env, capsys, as_json=True)
        assert code == 0
        by_hash = {r["content_hash"]: r for r in rows}
        assert by_hash[target.stem]["verified"] is False
        assert by_hash[target.stem]["bytes"] == 2
        assert sum(r["sessions"] for r in rows) == 2
        assert target.read_bytes() == b"xx"
        code, out = _snapshots(env, capsys)
        assert code == 0 and "NO" in out and "yes" in out
        assert out.splitlines()[0].startswith("content_hash")

    def test_empty(self, env: Env, capsys):
        code, out = _snapshots(env, capsys)
        assert code == 0 and out.strip() == "(no snapshots)"
        code, rows = _snapshots(env, capsys, as_json=True)
        assert code == 0 and rows == []

    def test_includes_fallback_directory(self, env: Env, capsys):
        _run(env, _pre(S1))
        fallback = cch._FALLBACK_SINK_DIR
        fallback.mkdir()
        cch.ClaudeCodeGovernanceHook(_pre(S2), fallback / f"{S2}.jsonl").process()
        code, rows = _snapshots(env, capsys, as_json=True)
        assert code == 0
        assert {r["directory"] for r in rows} == {str(env.sink_base), str(fallback)}
        assert all(r["sessions"] == 1 for r in rows)

    def test_shared_file_mode_directory(self, env: Env, capsys, monkeypatch):
        shared = env.sink_base / "shared.jsonl"
        env.sink_base = shared
        monkeypatch.setenv("SENTIENCE_CLAUDE_CODE_SINK_PATH", str(shared))
        _run(env, _pre(S1), shared=True)
        env.sink_base = shared.parent
        code, rows = _snapshots(env, capsys, as_json=True)
        assert code == 0 and len(rows) == 1 and rows[0]["sessions"] == 1


# ---------------------------------------------------------------------------
# validate [PATH] (retained)
# ---------------------------------------------------------------------------


class TestValidatePath:
    def test_explicit_valid_path(self, env: Env, tmp_path: Path, capsys):
        target = env.profiles / "A.yaml"
        before = target.read_bytes()
        code = _read_only(env, lambda: ux_mod.run_profile_validate(_ns(path=str(target))))
        assert code == 0
        assert target.read_bytes() == before
        assert "valid" in capsys.readouterr().out.lower()

    def test_explicit_missing_path_exits_one(self, env: Env, tmp_path: Path, capsys):
        code = ux_mod.run_profile_validate(_ns(path=str(tmp_path / "nope.yaml")))
        assert code == 1
        assert "failed to load" in capsys.readouterr().err

    def test_explicit_malformed_path_exits_one(self, env: Env, tmp_path: Path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text("schema_version: 1\nsession_intent: [not, a, mapping]\n", encoding="utf-8")
        code = ux_mod.run_profile_validate(_ns(path=str(bad)))
        assert code == 1

    def test_default_path_semantics_unchanged(self, env: Env, capsys, monkeypatch):
        monkeypatch.setattr(ux_mod, "DEFAULT_PROFILE_PATH", env.default)
        assert ux_mod.run_profile_validate(_ns()) == 0  # no file: defaults valid
        env.default.write_text(PROFILE_A, encoding="utf-8")
        assert _read_only(env, lambda: ux_mod.run_profile_validate(_ns())) == 0


# ---------------------------------------------------------------------------
# argparse contract through main()
# ---------------------------------------------------------------------------


class TestArgparse:
    @pytest.fixture(autouse=True)
    def _quiet_main(self, monkeypatch):
        monkeypatch.setenv("SENTIENCE_NO_FIRST_RUN_PROMPT", "1")

    def _main(self, monkeypatch, *argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["sentience", *argv])
        return ux_mod.main()

    def test_resolve_requires_exactly_one_target(self, env: Env, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            self._main(monkeypatch, "profile", "resolve")
        assert exc.value.code == 2
        with pytest.raises(SystemExit) as exc:
            self._main(monkeypatch, "profile", "resolve", "--agent-id", "a", "--session-id", "s")
        assert exc.value.code == 2

    def test_resolve_agent_id_json_via_main(self, env: Env, monkeypatch, capsys):
        code = _read_only(
            env, lambda: self._main(monkeypatch, "profile", "resolve", "--agent-id", "claude-code-x", "--json")
        )
        assert code == 0
        assert json.loads(capsys.readouterr().out)["resolution"] == "bound"

    def test_resolve_session_id_exit_codes_via_main(self, env: Env, monkeypatch, capsys):
        assert self._main(monkeypatch, "profile", "resolve", "--session-id", "missing") == 1
        capsys.readouterr()
        _run(env, _pre(S1))
        assert self._main(monkeypatch, "profile", "resolve", "--session-id", S1) == 0

    def test_snapshots_via_main(self, env: Env, monkeypatch, capsys):
        _run(env, _pre(S1))
        assert self._main(monkeypatch, "profile", "snapshots", "--json") == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 1 and rows[0]["sessions"] == 1
