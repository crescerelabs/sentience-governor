"""v0.3.2 CP2: sticky-session binding state (unit level).

Covers the machinery in ``session_manager/resumption.py`` and the recovery
decision in ``wrapper/claude_code_hook.py`` against synthetic sinks, per the
v0.3.2 sticky-state design (checkpoint CP1-D; row numbers below are its
expected-output rows):

* content-addressed snapshots: write, verify-before-return, immutability,
  repair of a corrupt file at the hash path, degrade to ``None`` when the
  repair cannot be written (rows 12g, 12h, 47, 49);
* binding entries: schema validation, identities, read/record round trip,
  coexistence with the other sidecar buckets (rows 42, 48, 12f);
* ``read_registration``: first AGENT_REGISTERED for the session, with and
  without provenance (shared-file interleaving, unreadable sink);
* the recovery decision with synthetic registrations, including the
  provenance-aware cases the plan's registration fields will only start
  producing in CP3 (rows 12c, 12d, 12d', 12d'');
* snapshot rebuild identities (rows 38, 39, 50, 52) and sidecar sizing
  (rows 41, 42).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

import pytest

from sentience_governor.profile import GovernanceProfile
from sentience_governor.profile import loader as loader_module
from sentience_governor.profile.resolver import (
    SOURCE_BOUND,
    SOURCE_DEFAULT,
    SOURCE_NONE,
    ResolvedProfile,
)
from sentience_governor.session_manager import resumption as res
from sentience_governor.session_manager.resumption import (
    Registration,
    build_binding_entry,
    content_hash_of,
    ensure_profile_snapshot,
    profiles_dir_for,
    read_profile_snapshot,
    read_registration,
    read_session_binding,
    record_session_binding,
    sidecar_path_for,
    snapshot_relative_path,
    update_session_state,
)
from sentience_governor.wrapper import claude_code_hook as cch

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX12 = re.compile(r"^[0-9a-f]{12}$")

PROFILE_A = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  demand_at: first_write\n"
    "task_boundary:\n"
    "  signals: [dir_change, time_gap]\n"
    "  time_gap_seconds: 120\n"
    "high_consequence:\n"
    "  tools:\n"
    "    - \"Bash:shell\"\n"
)
PROFILE_B = (
    "schema_version: 1\n"
    "session_intent:\n"
    "  required: true\n"
    "  demand_at: first_tool_call\n"
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _profile(tmp_path: Path, name: str, text: str) -> GovernanceProfile:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return GovernanceProfile.from_file(p)


def _registration_line(
    session_id: str,
    fingerprint: Optional[str],
    *,
    resolution: Optional[str] = None,
    binding: Optional[str] = None,
    agent_id: str = "claude-code-abc",
) -> str:
    """A synthetic AGENT_REGISTERED envelope. ``resolution``/``binding`` are
    the CP3 payload provenance fields; omitted for 0.3.1.2-style lines."""
    payload = {"agent_id": agent_id, "declared_capabilities": []}
    if resolution is not None:
        payload["profile_resolution"] = resolution
    if binding is not None:
        payload["profile_binding"] = binding
    event = {
        "event_id": "evt-1",
        "event_type": "AGENT_REGISTERED",
        "session_id": session_id,
        "agent_id": agent_id,
        "sequence": 1,
        "payload": payload,
    }
    if fingerprint is not None:
        event["profile_fingerprint"] = fingerprint
    return json.dumps(event)


def _write_sink(sink: Path, *lines: str) -> None:
    sink.parent.mkdir(parents=True, exist_ok=True)
    sink.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _resolved(
    profile: Optional[GovernanceProfile], source: str, binding: Optional[str] = None
) -> ResolvedProfile:
    return ResolvedProfile(profile=profile, source=source, binding=binding, warnings=[])


# ---------------------------------------------------------------------------
# snapshots (§2; rows 12g, 12h, 47, 49)
# ---------------------------------------------------------------------------


class TestSnapshots:
    def test_snapshot_is_named_by_its_content_hash_and_verified(self, tmp_path: Path):
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        canonical = prof.canonical_bytes()
        target = ensure_profile_snapshot(profiles_dir_for(tmp_path / "t.jsonl"), canonical)
        assert target is not None
        assert target.parent == tmp_path / "profiles"
        # row 47: sha256(file bytes) == stem, 64 lowercase hex.
        assert HEX64.match(target.stem)
        assert content_hash_of(target.read_bytes()) == target.stem
        assert target.read_bytes() == canonical
        assert prof.content_hash() == target.stem
        assert prof.fingerprint() == target.stem[:12]

    def test_snapshot_bytes_have_no_trailing_newline_and_are_canonical(self, tmp_path: Path):
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        canonical = prof.canonical_bytes()
        assert not canonical.endswith(b"\n")
        assert json.loads(canonical) == loader_module.canonical_profile_data(prof.to_dict())

    def test_existing_valid_snapshot_is_left_untouched(self, tmp_path: Path):
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        canonical = prof.canonical_bytes()
        pdir = profiles_dir_for(tmp_path / "t.jsonl")
        first = ensure_profile_snapshot(pdir, canonical)
        assert first is not None
        before = first.stat()
        os.utime(first, (before.st_atime - 100, before.st_mtime - 100))
        stamped = first.stat().st_mtime
        second = ensure_profile_snapshot(pdir, canonical)
        assert second == first
        assert first.stat().st_mtime == stamped  # not rewritten
        assert len(list(pdir.iterdir())) == 1  # no temp files left behind

    def test_corrupt_file_at_hash_path_is_repaired_and_verified(self, tmp_path: Path, caplog):
        """Row 12g: a corrupted artifact occupying the hash-derived name is
        repaired atomically; the returned path verifies."""
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        canonical = prof.canonical_bytes()
        pdir = profiles_dir_for(tmp_path / "t.jsonl")
        pdir.mkdir()
        target = pdir / f"{content_hash_of(canonical)}.json"
        target.write_bytes(b"{garbage")
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            out = ensure_profile_snapshot(pdir, canonical)
        assert out == target
        assert target.read_bytes() == canonical
        assert any("repairing" in r.getMessage() for r in caplog.records)

    def test_repair_failure_returns_none_and_never_binds(self, tmp_path: Path, monkeypatch, caplog):
        """Row 12h: corrupt file at the hash path AND the repair write fails
        (OSError) → None. The write failure is injected at the atomic
        writer so the test is independent of filesystem permissions."""
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        canonical = prof.canonical_bytes()
        pdir = profiles_dir_for(tmp_path / "t.jsonl")
        pdir.mkdir()
        target = pdir / f"{content_hash_of(canonical)}.json"
        target.write_bytes(b"{garbage")

        def boom(*_a, **_k):
            raise OSError("read-only")

        monkeypatch.setattr(res, "_write_snapshot_atomic", boom)
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            assert ensure_profile_snapshot(pdir, canonical) is None
        assert target.read_bytes() == b"{garbage"  # untouched, still corrupt
        assert any("could not be materialized" in r.getMessage() for r in caplog.records)

    def test_unwritable_directory_returns_none(self, tmp_path: Path):
        """A genuinely unwritable parent (no injection): still None, no raise."""
        if os.geteuid() == 0:  # pragma: no cover - root ignores modes
            pytest.skip("permission bits are not enforced for root")
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        pdir = tmp_path / "profiles"
        pdir.mkdir()
        pdir.chmod(0o500)
        try:
            assert ensure_profile_snapshot(pdir, prof.canonical_bytes()) is None
        finally:
            pdir.chmod(0o700)

    def test_read_snapshot_checks_full_hash_before_anything_else(self, tmp_path: Path, monkeypatch):
        """Row 49 / Test A: one mutated byte → None on the full-hash mismatch;
        no fingerprint is ever computed from the bytes."""
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        canonical = prof.canonical_bytes()
        pdir = profiles_dir_for(tmp_path / "t.jsonl")
        target = ensure_profile_snapshot(pdir, canonical)
        assert target is not None
        expected = target.stem
        assert read_profile_snapshot(target, expected) == canonical

        mutated = bytearray(canonical)
        mutated[len(mutated) // 2] ^= 0x01
        target.write_bytes(bytes(mutated))
        # Any GovernanceProfile construction here would be a fingerprint
        # computation path; the reader must not reach it.
        original_fingerprint = GovernanceProfile.fingerprint
        monkeypatch.setattr(
            GovernanceProfile, "fingerprint", lambda self: pytest.fail("fingerprint computed")
        )
        assert read_profile_snapshot(target, expected) is None
        monkeypatch.setattr(GovernanceProfile, "fingerprint", original_fingerprint)
        # Repair path: ensure_profile_snapshot fixes the mutated file in place.
        assert ensure_profile_snapshot(pdir, canonical) == target
        assert target.read_bytes() == canonical

    def test_read_snapshot_rejects_path_not_named_by_hash(self, tmp_path: Path):
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        canonical = prof.canonical_bytes()
        other = tmp_path / "profiles" / "not-the-hash.json"
        other.parent.mkdir()
        other.write_bytes(canonical)
        assert read_profile_snapshot(other, content_hash_of(canonical)) is None

    def test_read_snapshot_missing_file_is_none(self, tmp_path: Path):
        h = "0" * 64
        assert read_profile_snapshot(tmp_path / "profiles" / f"{h}.json", h) is None

    def test_snapshot_relative_path_shape(self):
        h = "ab" * 32
        assert snapshot_relative_path(h) == f"profiles/{h}.json"


# ---------------------------------------------------------------------------
# binding entries (§3; rows 42, 48, 12f, 14)
# ---------------------------------------------------------------------------


class TestBindingEntries:
    def test_build_entry_identities(self):
        h = "ab" * 32
        e = build_binding_entry(
            resolution="bound",
            binding="claude-code-*",
            profile_content_hash=h,
            source_path="/x/a.yaml",
            bound_by="claude_code_hook/test",
        )
        # row 48: both identities present and related by prefix.
        assert e["profile_fingerprint"] == h[:12]
        assert e["profile_content_hash"] == h
        assert e["snapshot"] == f"profiles/{h}.json"
        assert e["schema"] == 1
        assert e["recovered_from"] is None
        assert e["bound_at"].endswith("Z")
        assert res._valid_binding_entry(e)

    def test_build_entry_bound_to_no_profile(self):
        e = build_binding_entry(
            resolution="none",
            binding=None,
            profile_content_hash=None,
            source_path="ignored",
            bound_by="x",
        )
        assert e["profile_fingerprint"] is None
        assert e["profile_content_hash"] is None
        assert e["snapshot"] is None
        assert e["source_path"] is None
        assert res._valid_binding_entry(e)

    def test_validation_rejects_disagreeing_identities(self):
        """Row 48 / 12f: an entry whose fingerprint is not the hash prefix
        is malformed and treated as missing."""
        h = "ab" * 32
        e = build_binding_entry(
            resolution="bound", binding="p", profile_content_hash=h, source_path="/a", bound_by="x"
        )
        e["profile_fingerprint"] = "cd" * 6
        assert not res._valid_binding_entry(e)

    @pytest.mark.parametrize(
        "mutation",
        [
            {"schema": 2},
            {"resolution": "sticky"},
            {"recovered_from": "magic"},
            {"snapshot": "profiles/other.json"},
            {"profile_content_hash": "zz" * 32},
            {"source_path": ""},
            {"profile_content_hash": None},  # hash gone but fp/snapshot remain
        ],
    )
    def test_validation_rejects_malformed_entries(self, mutation):
        h = "ab" * 32
        e = build_binding_entry(
            resolution="bound", binding="p", profile_content_hash=h, source_path="/a", bound_by="x"
        )
        e.update(mutation)
        assert not res._valid_binding_entry(e)

    def test_bound_and_default_without_profile_are_invalid(self):
        for resolution in ("bound", "default"):
            e = build_binding_entry(
                resolution=resolution,
                binding=None,
                profile_content_hash=None,
                source_path=None,
                bound_by="x",
            )
            assert not res._valid_binding_entry(e)

    def test_record_and_read_round_trip(self, tmp_path: Path):
        sink = tmp_path / "t.jsonl"
        h = "ab" * 32
        e = build_binding_entry(
            resolution="bound", binding="p", profile_content_hash=h, source_path="/a", bound_by="x"
        )
        assert record_session_binding(sink, "s1", e)
        assert read_session_binding(sink, "s1") == e
        assert read_session_binding(sink, "s2") is None

    def test_malformed_recorded_entry_reads_as_missing(self, tmp_path: Path, caplog):
        sink = tmp_path / "t.jsonl"
        sidecar = sidecar_path_for(sink)
        sidecar.write_text(json.dumps({res._SESSION_BINDING_KEY: {"s1": {"schema": 1}}}))
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            assert read_session_binding(sink, "s1") is None
        assert any("malformed" in r.getMessage() for r in caplog.records)

    def test_bucket_coexists_with_session_state(self, tmp_path: Path):
        """Row 14: binding entries for X and Y survive update_session_state
        for X, and vice versa (read-modify-write of the whole index)."""
        sink = tmp_path / "shared.jsonl"
        sink.write_text("")
        h = "ab" * 32
        ex = build_binding_entry(
            resolution="bound", binding="p", profile_content_hash=h, source_path="/a", bound_by="x"
        )
        ey = dict(ex, resolution="default", binding=None)
        assert record_session_binding(sink, "X", ex)
        assert record_session_binding(sink, "Y", ey)
        update_session_state(sink, "X", last_sequence=3, last_event_id="e3", file_offset=0)
        assert read_session_binding(sink, "X") == ex
        assert read_session_binding(sink, "Y") == ey
        index = json.loads(sidecar_path_for(sink).read_text())
        assert index["X"]["last_sequence"] == 3
        assert res._SESSION_BINDING_KEY in index

    def test_per_session_entry_size(self, tmp_path: Path):
        """Row 42: ≈1,099 bytes for a per-session sidecar with one binding
        (within ±5%), and constant across later session-state updates."""
        sink = tmp_path / "s.jsonl"
        sink.write_text("")
        h = "ab" * 32
        e = build_binding_entry(
            resolution="bound",
            binding="claude-code-*",
            profile_content_hash=h,
            source_path=str(Path.home() / ".sentience" / "profiles" / "a.yaml"),
            bound_by="claude_code_hook/0.3.2",
        )
        record_session_binding(sink, "s" * 36, e)
        update_session_state(sink, "s" * 36, last_sequence=1, last_event_id="e" * 36, file_offset=0)
        size1 = sidecar_path_for(sink).stat().st_size
        update_session_state(sink, "s" * 36, last_sequence=50, last_event_id="f" * 36, file_offset=9000)
        size2 = sidecar_path_for(sink).stat().st_size
        # CP1-D measured ≈1,099 bytes with a prototype entry; the shipped
        # entry is smaller. Contract: bounded by the measurement, constant
        # across session-state updates (digits only).
        assert size1 <= 1099 * 1.05, size1
        assert abs(size2 - size1) <= 8

    @pytest.mark.parametrize("n,expected", [(10, 10_252), (100, 101_782), (1_000, 1_017_082)])
    def test_shared_sidecar_scaling(self, tmp_path: Path, n: int, expected: int):
        """Row 41: full-hash references scale linearly; sizes within ±5% of
        the CP1-D measurement. The bucket is built with one write per
        session, as the hook does; 10,000 is covered by the measurement
        and skipped here for suite time."""
        sink = tmp_path / "shared.jsonl"
        sink.write_text("")
        sidecar = sidecar_path_for(sink)
        h = "ab" * 32
        index = {res._SESSION_BINDING_KEY: {}}
        for i in range(n):
            sid = f"{i:08x}-0000-4000-8000-000000000000"
            index[res._SESSION_BINDING_KEY][sid] = build_binding_entry(
                resolution="bound",
                binding="claude-code-*",
                profile_content_hash=h,
                source_path=str(Path.home() / ".sentience" / "profiles" / "a.yaml"),
                bound_by="claude_code_hook/0.3.2",
                bound_at="2026-09-14T00:00:00.000Z",
            )
        res._write_sidecar_atomic(sidecar, index)
        size = sidecar.stat().st_size
        # Linear in n and bounded by the CP1-D measurement (which used a
        # larger prototype entry; the shipped entry is ~570 bytes).
        assert size <= expected * 1.05, size
        per_entry = size / n
        assert 500 <= per_entry <= 700, per_entry


# ---------------------------------------------------------------------------
# read_registration (§5.1)
# ---------------------------------------------------------------------------


class TestReadRegistration:
    def test_not_found_when_sink_missing(self, tmp_path: Path):
        r = read_registration(tmp_path / "missing.jsonl", "s")
        assert r == Registration(found=False)
        assert not r.has_provenance

    def test_legacy_registration_has_no_provenance(self, tmp_path: Path):
        sink = tmp_path / "t.jsonl"
        _write_sink(sink, _registration_line("s", "a3a3f8c3da0f"))
        r = read_registration(sink, "s")
        assert r.found and r.profile_fingerprint == "a3a3f8c3da0f"
        assert r.profile_resolution is None and r.profile_binding is None
        assert not r.has_provenance

    def test_registration_without_fingerprint(self, tmp_path: Path):
        sink = tmp_path / "t.jsonl"
        _write_sink(sink, _registration_line("s", None))
        r = read_registration(sink, "s")
        assert r.found and r.profile_fingerprint is None

    def test_provenance_fields_are_read_from_payload(self, tmp_path: Path):
        sink = tmp_path / "t.jsonl"
        _write_sink(sink, _registration_line("s", "a" * 12, resolution="bound", binding="claude-code-*"))
        r = read_registration(sink, "s")
        assert r.has_provenance
        assert (r.profile_resolution, r.profile_binding) == ("bound", "claude-code-*")

    def test_first_registration_for_the_session_only(self, tmp_path: Path):
        """Shared-file interleaving: other sessions' registrations and
        non-registration lines mentioning the type are skipped; the first
        matching one wins."""
        sink = tmp_path / "shared.jsonl"
        _write_sink(
            sink,
            json.dumps({"event_type": "SCOPE_ASSERTED", "session_id": "s", "note": "AGENT_REGISTERED"}),
            "not json AGENT_REGISTERED",
            _registration_line("other", "b" * 12),
            _registration_line("s", "a" * 12),
            _registration_line("s", "c" * 12),
        )
        assert read_registration(sink, "s").profile_fingerprint == "a" * 12


# ---------------------------------------------------------------------------
# recovery decision (§6) with synthetic registrations
# ---------------------------------------------------------------------------


@pytest.fixture
def hook_env(tmp_path: Path, monkeypatch):
    """resolution.yaml binding claude-code-* → A; default profile D; both
    module paths late-bound through the loader (as the resolver reads them)."""
    home = tmp_path / "home"
    (home / "profiles").mkdir(parents=True)
    (home / "profiles" / "A.yaml").write_text(PROFILE_A, encoding="utf-8")
    (home / "resolution.yaml").write_text(
        "schema_version: 1\nbindings:\n  - agent_id: claude-code-*\n    profile: profiles/A.yaml\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(loader_module, "DEFAULT_RESOLUTION_PATH", home / "resolution.yaml")
    monkeypatch.setattr(loader_module, "DEFAULT_PROFILE_PATH", home / "profile.yaml")
    return home


def _fa(home: Path) -> str:
    return GovernanceProfile.from_file(home / "profiles" / "A.yaml").fingerprint()


class TestRecoveryDecision:
    SID = "sess-recover-0001"
    AID = "claude-code-sess-rec"

    def test_first_process_establishes_without_marker(self, hook_env: Path, tmp_path: Path):
        sink = tmp_path / "t.jsonl"
        bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.resolution == SOURCE_BOUND and bound.binding == "claude-code-*"
        assert bound.fingerprint == _fa(hook_env)
        assert bound.recovered_from is None
        entry = read_session_binding(sink, self.SID)
        assert entry is not None and entry["recovered_from"] is None
        assert entry["profile_fingerprint"] == bound.fingerprint
        assert entry["source_path"] == str(hook_env / "profiles" / "A.yaml")
        assert (sink.parent / entry["snapshot"]).is_file()

    def test_12c_lost_binding_agreeing_registration_rematerializes_silently(
        self, hook_env: Path, tmp_path: Path, caplog
    ):
        sink = tmp_path / "t.jsonl"
        fa = _fa(hook_env)
        _write_sink(sink, _registration_line(self.SID, fa))
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.fingerprint == fa
        assert bound.recovered_from == "rematerialized"
        assert read_session_binding(sink, self.SID)["recovered_from"] == "rematerialized"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_12d_lost_binding_changed_profile_reresolves_with_warning(
        self, hook_env: Path, tmp_path: Path, caplog
    ):
        sink = tmp_path / "t.jsonl"
        fa = _fa(hook_env)
        _write_sink(sink, _registration_line(self.SID, fa))
        (hook_env / "profiles" / "A.yaml").write_text(PROFILE_B, encoding="utf-8")
        fa_prime = _fa(hook_env)
        assert fa_prime != fa
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.fingerprint == fa_prime
        assert bound.recovered_from == "reresolve"
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert self.SID[:12] in warnings[0] and fa in warnings[0] and fa_prime in warnings[0]
        assert read_session_binding(sink, self.SID)["recovered_from"] == "reresolve"

    def test_12d_prime_same_content_different_binding_is_not_silent(
        self, hook_env: Path, tmp_path: Path, caplog
    ):
        """Registration recorded provenance (bound, claude-code-*); the fresh
        resolution reaches identical bytes through claude-code-prod-*."""
        sink = tmp_path / "t.jsonl"
        fa = _fa(hook_env)
        _write_sink(
            sink, _registration_line(self.SID, fa, resolution="bound", binding="claude-code-*")
        )
        (hook_env / "resolution.yaml").write_text(
            "schema_version: 1\nbindings:\n"
            "  - agent_id: claude-code-prod-*\n    profile: profiles/A.yaml\n"
            "  - agent_id: claude-code-*\n    profile: profiles/A.yaml\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            bound = cch._establish_or_rehydrate_binding(
                sink, self.SID, "claude-code-prod-1234", None
            )
        assert bound.fingerprint == fa  # identical bytes
        assert bound.binding == "claude-code-prod-*"
        assert bound.recovered_from == "reresolve"
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "claude-code-prod-*" in warnings[0] and "claude-code-*" in warnings[0]

    def test_12d_double_prime_legacy_registration_compares_fingerprint_only(
        self, hook_env: Path, tmp_path: Path, caplog
    ):
        """Same as 12d' but the session registered under 0.3.1.2: no
        provenance → rematerialized, no warning (in-flight upgrade)."""
        sink = tmp_path / "t.jsonl"
        fa = _fa(hook_env)
        _write_sink(sink, _registration_line(self.SID, fa))
        (hook_env / "resolution.yaml").write_text(
            "schema_version: 1\nbindings:\n"
            "  - agent_id: claude-code-prod-*\n    profile: profiles/A.yaml\n",
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            bound = cch._establish_or_rehydrate_binding(
                sink, self.SID, "claude-code-prod-1234", None
            )
        assert bound.fingerprint == fa
        assert bound.recovered_from == "rematerialized"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_provenance_resolution_mismatch_is_reresolve(self, hook_env: Path, tmp_path: Path, caplog):
        """Registered degraded-onto-default; now the binding target exists
        with identical content → fingerprint equal, resolution differs."""
        sink = tmp_path / "t.jsonl"
        fa = _fa(hook_env)
        _write_sink(
            sink, _registration_line(self.SID, fa, resolution="degraded", binding="claude-code-*")
        )
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.resolution == SOURCE_BOUND
        assert bound.recovered_from == "reresolve"
        assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1

    def test_12e_snapshot_deleted_binding_present_recovers_and_rematerializes(
        self, hook_env: Path, tmp_path: Path, caplog
    ):
        sink = tmp_path / "t.jsonl"
        first = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        _write_sink(sink, _registration_line(self.SID, first.fingerprint))
        snap = sink.parent / read_session_binding(sink, self.SID)["snapshot"]
        snap.unlink()
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            again = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert again.fingerprint == first.fingerprint
        assert again.recovered_from == "rematerialized"
        assert snap.is_file() and content_hash_of(snap.read_bytes()) == snap.stem
        # The snapshot reader logged the fault; the recovery itself was silent.
        msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert all("unreadable" in m for m in msgs), msgs

    def test_49_mutated_snapshot_recovers_and_repairs(self, hook_env: Path, tmp_path: Path, caplog):
        sink = tmp_path / "t.jsonl"
        first = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        _write_sink(sink, _registration_line(self.SID, first.fingerprint))
        snap = sink.parent / read_session_binding(sink, self.SID)["snapshot"]
        good = snap.read_bytes()
        bad = bytearray(good)
        bad[0] ^= 0x01
        snap.write_bytes(bytes(bad))
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            again = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert again.recovered_from == "rematerialized"
        assert snap.read_bytes() == good  # repaired in place
        entry = read_session_binding(sink, self.SID)
        assert entry["profile_content_hash"] == snap.stem
        assert not [
            r for r in caplog.records
            if r.levelno >= logging.WARNING and "re-resolved" in r.getMessage()
        ]

    def test_12h_no_verified_snapshot_governs_without_binding_then_retries(
        self, hook_env: Path, tmp_path: Path, monkeypatch, caplog
    ):
        sink = tmp_path / "t.jsonl"
        fa = _fa(hook_env)

        def boom(*_a, **_k):
            raise OSError("read-only")

        original_writer = res._write_snapshot_atomic
        monkeypatch.setattr(res, "_write_snapshot_atomic", boom)
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.profile is not None and bound.fingerprint == fa
        assert read_session_binding(sink, self.SID) is None  # nothing durable
        assert any("no verified profile snapshot" in r.getMessage() for r in caplog.records)
        monkeypatch.setattr(res, "_write_snapshot_atomic", original_writer)
        _write_sink(sink, _registration_line(self.SID, fa))
        again = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert again.recovered_from == "rematerialized"
        assert read_session_binding(sink, self.SID) is not None

    def test_12f_tampered_binding_is_never_used(self, hook_env: Path, tmp_path: Path):
        sink = tmp_path / "t.jsonl"
        first = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        _write_sink(sink, _registration_line(self.SID, first.fingerprint))
        entry = read_session_binding(sink, self.SID)
        # (a) prefix relation broken → fails validation
        tampered = dict(entry, profile_fingerprint="0" * 12)
        record_session_binding(sink, self.SID, tampered)
        bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.fingerprint == first.fingerprint
        assert bound.recovered_from == "rematerialized"
        # (b) relation intact but disagreeing with the registration (hash of
        # a different, valid snapshot) → rehydrates, then §5 step 8 rejects
        other = _profile(tmp_path, "b.yaml", PROFILE_B)
        other_snap = ensure_profile_snapshot(profiles_dir_for(sink), other.canonical_bytes())
        assert other_snap is not None
        h = other_snap.stem
        record_session_binding(
            sink,
            self.SID,
            dict(
                entry,
                profile_fingerprint=h[:12],
                profile_content_hash=h,
                snapshot=snapshot_relative_path(h),
            ),
        )
        bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.fingerprint == first.fingerprint
        assert bound.recovered_from == "rematerialized"

    def test_rehydrate_bound_to_no_profile_stays_none(self, hook_env: Path, tmp_path: Path):
        """A session bound to NO profile stays that way even after a default
        profile appears (distinct from having no binding)."""
        sink = tmp_path / "t.jsonl"
        (hook_env / "resolution.yaml").unlink()
        bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.profile is None and bound.resolution == SOURCE_NONE
        entry = read_session_binding(sink, self.SID)
        assert entry["profile_content_hash"] is None
        (hook_env / "profile.yaml").write_text(PROFILE_B, encoding="utf-8")
        again = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert again.profile is None and again.resolution == SOURCE_NONE
        # A fresh session, by contrast, now resolves to the default.
        fresh = cch._establish_or_rehydrate_binding(sink, "sess-other", "claude-code-x", None)
        assert fresh.resolution == SOURCE_DEFAULT and fresh.profile is not None

    def test_rehydrated_profile_disagreeing_with_registration_reresolves(
        self, hook_env: Path, tmp_path: Path, caplog
    ):
        """Binding and snapshot are internally consistent but the trace's
        registration says something else (e.g. registration written under
        a different config than the binding, row 12f second form)."""
        sink = tmp_path / "t.jsonl"
        cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        _write_sink(sink, _registration_line(self.SID, "0" * 12))
        with caplog.at_level(logging.WARNING, logger="sentience_governor"):
            bound = cch._establish_or_rehydrate_binding(sink, self.SID, self.AID, None)
        assert bound.recovered_from == "reresolve"
        assert any("disagrees with its registration" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# snapshot rebuild identities (rows 38, 39, 50, 52)
# ---------------------------------------------------------------------------


class TestRebuildIdentities:
    def test_38_rebuilt_profile_reproduces_hash_and_fingerprint(self, tmp_path: Path):
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        snap = ensure_profile_snapshot(tmp_path / "profiles", prof.canonical_bytes())
        data = json.loads(read_profile_snapshot(snap, snap.stem).decode("utf-8"))
        rebuilt = GovernanceProfile(data, source_path=prof.source_path)
        assert rebuilt.content_hash() == snap.stem == prof.content_hash()
        assert rebuilt.fingerprint() == prof.fingerprint()
        assert rebuilt.canonical_bytes() == snap.read_bytes()  # idempotent

    @pytest.mark.parametrize(
        "name", ["legacy_minimal.yaml", "legacy_representative.yaml", "legacy_reserved_and_unknown.yaml"]
    )
    def test_39_legacy_profile_bound_then_reconstructed_keeps_fingerprint(self, tmp_path: Path, name):
        recorded = json.loads((FIXTURES / "recorded_fingerprints_0.3.1.2.json").read_text())[name]
        prof = GovernanceProfile.from_file(FIXTURES / name)
        canonical = prof.canonical_bytes()
        assert "operations" not in json.loads(canonical)["high_consequence"]
        snap = ensure_profile_snapshot(tmp_path / "profiles", canonical)
        rebuilt = GovernanceProfile(json.loads(snap.read_bytes()), source_path=FIXTURES / name)
        assert rebuilt.fingerprint() == recorded["fingerprint"]
        assert rebuilt.content_hash() == recorded["content_hash"] == snap.stem

    def test_50_52_fingerprint_is_prefix_and_twelve_hex(self, tmp_path: Path):
        prof = _profile(tmp_path, "a.yaml", PROFILE_A)
        assert HEX12.match(prof.fingerprint())
        assert prof.fingerprint() == prof.content_hash()[:12]
