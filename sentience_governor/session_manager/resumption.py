"""Sink-backed session resumption primitive.

Purpose
-------
Reconstruct per-session chain state (``event_sequence_number``,
``previous_event_id``) from an on-disk JSONL trace so a governance
wrapper running in a fresh process can pick up where a prior process
left off without breaking the chain.

Background
----------
The Claude Code hook adapter spawns a new Python process for every
tool invocation. Without disk-based resumption, sequence numbers
restart at ``0`` on every call and ``previous_event_id`` chains
break. This module solves that by:

* Reading the last event for a given ``session_id`` from the sink.
* Optionally using a sidecar index file for O(1) seek to the last
  event rather than scanning the whole sink.
* Self-healing: if the sidecar is missing, stale, or corrupt, the
  primitive falls back to a linear scan and repairs the sidecar.

Contract
--------
* **Sink is the source of truth.** Nothing in this module writes
  governance events; it only reads the sink and maintains the
  sidecar. If the sidecar contradicts the sink, the sink wins.
* **Fail-open.** Any I/O error, corruption, or lock-acquisition
  failure falls back to the linear-scan path and, if that also
  fails, returns ``None`` (meaning "treat this as a new session").
* **Atomic sidecar writes.** The sidecar is written to a temp file,
  ``fsync``-ed, then ``os.rename``-d into place. A crash mid-write
  leaves the prior sidecar intact; never a partially-written JSON
  file.
* **Locking.** Callers are expected to hold a file lock on the sink
  across the read + append + sidecar-update critical section. This
  module provides the ``sink_lock()`` context manager for that
  purpose. On platforms where ``fcntl`` is unavailable (e.g.
  Windows), the lock becomes a no-op and a warning is logged; the
  caller proceeds without locking.

Relationship to other adapters
------------------------------
The Claude Code hook uses this module today. The MCP wrapper will
adopt it when the Parking Lot item for process-restart continuity
is pulled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

try:
    import fcntl as _fcntl  # type: ignore
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows fallback
    _fcntl = None  # type: ignore
    _HAVE_FCNTL = False


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResumedState:
    """Chain state recovered for a given session_id.

    ``file_offset`` records the byte offset of the start of the line
    that produced this state. It is used by the sidecar for O(1)
    forward-validation on the next invocation.
    """

    session_id: str
    last_sequence: int
    last_event_id: str
    file_offset: int


# ---------------------------------------------------------------------------
# Sidecar path derivation
# ---------------------------------------------------------------------------


def sidecar_path_for(sink_path: Path) -> Path:
    """Return the conventional sidecar location next to ``sink_path``."""
    return Path(str(sink_path) + ".index")


# ---------------------------------------------------------------------------
# File locking (sink-scoped)
# ---------------------------------------------------------------------------


@contextmanager
def sink_lock(sink_path: Path) -> Iterator[None]:
    """Acquire an exclusive flock on the sink file for the duration.

    The lock covers the entire critical section: reading the sink,
    reading the sidecar, appending to the sink, and replacing the
    sidecar. No caller should ever read the sink or the sidecar
    outside this lock when session resumption is in play.

    On platforms without fcntl (Windows, some network filesystems),
    the lock becomes a no-op and a one-time warning is logged. Callers
    accept the small concurrency risk on those platforms; the
    alternative is hard-failing and blocking the host agent, which
    violates fail-open discipline.

    The sink file is created (empty) if it does not yet exist, so the
    lock has something to grab onto.
    """
    sink_path.parent.mkdir(parents=True, exist_ok=True)
    sink_path.touch(exist_ok=True)

    if not _HAVE_FCNTL:
        logger.warning(
            "fcntl unavailable on this platform; sink_lock is a no-op. "
            "Concurrent writes to %s may race.",
            sink_path,
        )
        yield
        return

    # Open with os.O_RDWR so we have a descriptor to lock. Do NOT use
    # the write path here — the caller opens the sink separately for
    # appending.
    fd = os.open(str(sink_path), os.O_RDWR)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)  # type: ignore[attr-defined]
        try:
            yield
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)  # type: ignore[attr-defined]
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Sidecar I/O
# ---------------------------------------------------------------------------


def _read_sidecar(sidecar: Path) -> dict:
    """Read the sidecar JSON; return ``{}`` on any error.

    Treats missing, empty, corrupt, and unparseable files identically:
    the sidecar is advisory, and any failure triggers a linear scan.
    """
    if not sidecar.exists():
        return {}
    try:
        raw = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("sidecar read failed for %s: %s", sidecar, exc)
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("sidecar JSON corrupt at %s: %s", sidecar, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("sidecar at %s is not a dict; ignoring", sidecar)
        return {}
    return data


def _write_sidecar_atomic(sidecar: Path, data: dict) -> None:
    """Write the sidecar atomically: temp → fsync → rename.

    A crash between ``write`` and ``rename`` leaves the prior sidecar
    intact. A crash after ``rename`` is indistinguishable from a
    successful completion. There is no intermediate state where a
    partially-written JSON file is visible under the sidecar path.
    """
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    tmp = sidecar.with_suffix(sidecar.suffix + ".tmp")
    payload = json.dumps(data, indent=2, sort_keys=True).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(str(tmp), str(sidecar))


# ---------------------------------------------------------------------------
# Sink scan / forward-validation
# ---------------------------------------------------------------------------


def _validate_at_offset(
    sink_path: Path,
    session_id: str,
    expected_sequence: int,
    expected_event_id: str,
    offset: int,
) -> bool:
    """Check that the event starting at ``offset`` matches what the
    sidecar records.

    Returns False on any mismatch, I/O error, or parse error. A False
    result always triggers the linear-scan fallback.
    """
    try:
        with open(sink_path, "rb") as fh:
            fh.seek(offset)
            line = fh.readline()
    except OSError:
        return False
    if not line:
        return False
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    return (
        event.get("session_id") == session_id
        and event.get("event_sequence_number") == expected_sequence
        and event.get("event_id") == expected_event_id
    )


def _scan_sink_for_session(
    sink_path: Path, session_id: str
) -> Optional[ResumedState]:
    """Linear scan: walk the sink forward, return last event for session_id.

    O(N) in sink size. Used as fallback when the sidecar is unusable
    and as the authoritative ground truth when validating a sidecar
    entry.
    """
    if not sink_path.exists():
        return None
    last: Optional[ResumedState] = None
    try:
        with open(sink_path, "rb") as fh:
            offset = 0
            while True:
                line_start = offset
                line = fh.readline()
                if not line:
                    break
                offset += len(line)
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Tolerate partial/corrupt trailing lines; keep scanning
                    continue
                if event.get("session_id") != session_id:
                    continue
                seq = event.get("event_sequence_number")
                eid = event.get("event_id")
                if not isinstance(seq, int) or not isinstance(eid, str):
                    continue
                last = ResumedState(
                    session_id=session_id,
                    last_sequence=seq,
                    last_event_id=eid,
                    file_offset=line_start,
                )
    except OSError as exc:
        logger.warning("sink scan failed for %s: %s", sink_path, exc)
        return None
    return last


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resume_session_state(
    sink_path: Path, session_id: str
) -> Optional[ResumedState]:
    """Return the last-known chain state for ``session_id``, or None.

    ``None`` means "no prior event found — treat this as a new
    session". This covers three cases that callers MUST handle
    identically:

    * sink file does not exist
    * sink file exists but contains no events for this session_id
    * sidecar and sink are both unusable (rare; fail-open)

    The sidecar, if present, is used as an O(1) hint and validated by
    reading the event at the recorded offset. Any mismatch falls back
    to a full linear scan; the sidecar entry is NOT automatically
    repaired here — callers should call ``update_session_state`` after
    appending the new event to record the fresh offset.
    """
    sidecar = sidecar_path_for(sink_path)
    index = _read_sidecar(sidecar)
    entry = index.get(session_id) if isinstance(index, dict) else None

    if (
        isinstance(entry, dict)
        and isinstance(entry.get("file_offset"), int)
        and isinstance(entry.get("last_sequence"), int)
        and isinstance(entry.get("last_event_id"), str)
    ):
        if _validate_at_offset(
            sink_path=sink_path,
            session_id=session_id,
            expected_sequence=entry["last_sequence"],
            expected_event_id=entry["last_event_id"],
            offset=entry["file_offset"],
        ):
            return ResumedState(
                session_id=session_id,
                last_sequence=entry["last_sequence"],
                last_event_id=entry["last_event_id"],
                file_offset=entry["file_offset"],
            )
        logger.info(
            "sidecar drift detected for session %s at %s; rebuilding from sink",
            session_id,
            sink_path,
        )

    return _scan_sink_for_session(sink_path, session_id)


def update_session_state(
    sink_path: Path,
    session_id: str,
    last_sequence: int,
    last_event_id: str,
    file_offset: int,
) -> None:
    """Record the most recent event for ``session_id`` in the sidecar.

    Must be called under ``sink_lock`` to guarantee the recorded
    offset reflects the sink's post-append state. Failures are logged
    and swallowed — the sidecar is an optimization, not the source of
    truth.
    """
    sidecar = sidecar_path_for(sink_path)
    try:
        index = _read_sidecar(sidecar)
        index[session_id] = {
            "last_sequence": last_sequence,
            "last_event_id": last_event_id,
            "file_offset": file_offset,
        }
        _write_sidecar_atomic(sidecar, index)
    except OSError as exc:
        logger.warning("sidecar update failed for %s: %s", sidecar, exc)


# ---------------------------------------------------------------------------
# Emitted-turn idempotency (v0.2.6.1 — Claude Code SessionEnd token batch)
# ---------------------------------------------------------------------------
#
# The SessionEnd token batch emits one snapshot per transcript ``requestId``.
# SessionEnd can fire more than once (re-runs, retries); re-emitting would
# append duplicate turns. We record the ``requestId``s already emitted under a
# sentinel sidecar key so a repeat run skips them. This is the FIRST line of
# idempotency defence; the analyzer's ``(session_id, llm_turn_id)`` dedupe is
# the second. The sentinel key cannot collide with a real ``session_id`` (it
# contains characters Claude Code session ids never use) and is preserved by
# ``update_session_state`` because that function reads-modifies-writes the
# whole index.

_EMITTED_TURNS_KEY = "__sentience_emitted_turns__"


def read_emitted_turns(sink_path: Path, session_id: str) -> set:
    """Return the set of ``requestId``s already emitted for ``session_id``.

    Empty set on any error or absence. Call under ``sink_lock``.
    """
    sidecar = sidecar_path_for(sink_path)
    index = _read_sidecar(sidecar)
    bucket = index.get(_EMITTED_TURNS_KEY)
    if not isinstance(bucket, dict):
        return set()
    ids = bucket.get(session_id)
    if not isinstance(ids, list):
        return set()
    return {i for i in ids if isinstance(i, str)}


def record_emitted_turns(
    sink_path: Path, session_id: str, request_ids: Iterable[str]
) -> None:
    """Merge ``request_ids`` into the emitted-turns record for ``session_id``.

    Must be called under ``sink_lock``. Failures are logged and swallowed —
    a sidecar write failure must never break session end (fail-open). The
    analyzer dedupe remains as a second line of defence.
    """
    sidecar = sidecar_path_for(sink_path)
    try:
        index = _read_sidecar(sidecar)
        bucket = index.get(_EMITTED_TURNS_KEY)
        if not isinstance(bucket, dict):
            bucket = {}
        existing = bucket.get(session_id)
        merged = set(existing) if isinstance(existing, list) else set()
        merged.update(r for r in request_ids if isinstance(r, str))
        bucket[session_id] = sorted(merged)
        index[_EMITTED_TURNS_KEY] = bucket
        _write_sidecar_atomic(sidecar, index)
    except OSError as exc:
        logger.warning("emitted-turns update failed for %s: %s", sidecar, exc)


def file_size(sink_path: Path) -> int:
    """Return current sink size in bytes, or 0 if missing.

    Convenience helper for callers that want to record the append
    offset without an extra ``stat`` call.
    """
    try:
        return sink_path.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# v0.3.2 — sticky session binding: content-addressed profile snapshots and
# the per-session binding bucket.
# ---------------------------------------------------------------------------
#
# The Claude Code hook runs one process per invocation. Per-session policy
# resolution (profile/resolver.py) is a pure function of configuration, so a
# configuration edit mid-session would silently change which policy governs
# the session's later events. To keep a session bound to the policy CONTENT
# it started under, the first process materializes the resolved profile's
# canonical bytes into an immutable, content-addressed snapshot file beside
# the sink and records a per-session binding in the sidecar; every later
# process rebuilds the identical GovernanceProfile from those two artifacts.
#
# Invariants:
#   * a snapshot file's name is the full SHA-256 of its bytes, and the bytes
#     are exactly the profile's canonical bytes (loader.canonical_bytes);
#   * ensure_profile_snapshot returns a path ONLY after verifying the file's
#     bytes hash to the expected value, so no durable binding ever references
#     an unverified snapshot;
#   * a file at the hash-derived name whose bytes do not hash to that name is
#     a corrupted artifact, not a valid snapshot; replacing it is repair, not
#     mutation of an immutable snapshot;
#   * a valid snapshot is never rewritten and never deleted by the runtime;
#   * the sidecar bucket is read-modify-write like the emitted-turns bucket,
#     so every existing sidecar writer preserves it and it preserves theirs;
#   * nothing here raises into the hook; every failure degrades to "no
#     verified snapshot" / "no usable binding" and is logged once.

_SESSION_BINDING_KEY = "__sentience_session_binding__"
SESSION_BINDING_SCHEMA = 1
PROFILES_DIR_NAME = "profiles"

BINDING_RESOLUTIONS = frozenset(["bound", "degraded", "default", "none"])
BINDING_RECOVERY_MARKERS = frozenset(["rematerialized", "reresolve"])

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def profiles_dir_for(sink_path: Path) -> Path:
    """Return the snapshot directory beside ``sink_path``: ``<parent>/profiles``.

    Derived from the sink the same way the sidecar is, so per-session mode,
    shared-file mode and the /tmp fallback all get one rule.
    """
    return Path(sink_path).parent / PROFILES_DIR_NAME


def snapshot_relative_path(content_hash: str) -> str:
    """The binding's ``snapshot`` value for ``content_hash``: ``profiles/<hash>.json``."""
    return f"{PROFILES_DIR_NAME}/{content_hash}.json"


def content_hash_of(canonical: bytes) -> str:
    return hashlib.sha256(canonical).hexdigest()


def _verify_snapshot_file(target: Path, expected_hash: str) -> bool:
    """True iff ``target`` exists and its bytes hash to ``expected_hash``."""
    try:
        data = target.read_bytes()
    except OSError:
        return False
    return content_hash_of(data) == expected_hash


def _write_snapshot_atomic(target: Path, canonical: bytes) -> None:
    """temp (unique) → write → fsync → os.replace. Raises OSError on failure."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.stem}.", suffix=".tmp", dir=str(target.parent)
    )
    try:
        try:
            os.write(fd, canonical)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, str(target))
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_profile_snapshot(profiles_dir: Path, canonical: bytes) -> Optional[Path]:
    """Materialize ``canonical`` as a verified, content-addressed snapshot.

    Returns the snapshot path ONLY if, at the moment of return, the file's
    bytes hash to ``sha256(canonical)``. A caller may write a session
    binding only against a returned path.

    * target absent: written atomically, re-read, verified;
    * target present and verifying: left untouched (immutability);
    * target present and NOT verifying: a corrupted artifact occupying the
      hash-derived name; repaired atomically with the canonical bytes, then
      verified;
    * any failure: ``None`` (logged once); the caller must not bind.
    """
    expected = content_hash_of(canonical)
    target = Path(profiles_dir) / f"{expected}.json"
    try:
        if target.exists():
            if _verify_snapshot_file(target, expected):
                return target
            logger.warning(
                "profile snapshot %s does not hash to its name; repairing the "
                "corrupted artifact in place",
                target.name,
            )
        _write_snapshot_atomic(target, canonical)
        if _verify_snapshot_file(target, expected):
            return target
        logger.warning(
            "profile snapshot %s failed verification after write; no binding "
            "will reference it",
            target.name,
        )
        return None
    except OSError as exc:
        logger.warning(
            "profile snapshot %s could not be materialized: %s; no binding "
            "will reference it",
            target.name,
            exc,
        )
        return None


def read_profile_snapshot(path: Path, expected_hash: str) -> Optional[bytes]:
    """Return the snapshot bytes iff they hash to ``expected_hash`` AND the
    file is named by that hash. ``None`` (logged) on any mismatch or error.

    This is the full-hash integrity decision. It is made before any short
    fingerprint is computed or compared.
    """
    path = Path(path)
    if path.stem != expected_hash:
        logger.warning("profile snapshot path %s is not named by the expected hash", path.name)
        return None
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("profile snapshot %s unreadable: %s", path.name, exc)
        return None
    if content_hash_of(data) != expected_hash:
        logger.warning(
            "profile snapshot %s is corrupted: bytes do not hash to the "
            "recorded profile_content_hash",
            path.name,
        )
        return None
    return data


def _valid_binding_entry(entry: Any) -> bool:
    """Validate a binding entry per the CP1-D schema; malformed → unusable."""
    if not isinstance(entry, dict):
        return False
    if entry.get("schema") != SESSION_BINDING_SCHEMA:
        return False
    resolution = entry.get("resolution")
    if resolution not in BINDING_RESOLUTIONS:
        return False
    fp = entry.get("profile_fingerprint")
    ch = entry.get("profile_content_hash")
    snap = entry.get("snapshot")
    src = entry.get("source_path")
    recovered = entry.get("recovered_from")
    if recovered is not None and recovered not in BINDING_RECOVERY_MARKERS:
        return False
    if fp is None and ch is None and snap is None and src is None:
        # Bound to no profile: only degraded (no default) or none may say so.
        return resolution in ("degraded", "none")
    if not (isinstance(ch, str) and _HEX64.match(ch)):
        return False
    if not (isinstance(fp, str) and fp == ch[:12]):
        return False
    if not (isinstance(snap, str) and snap == snapshot_relative_path(ch)):
        return False
    if not (isinstance(src, str) and src):
        return False
    return True


def read_session_binding(sink_path: Path, session_id: str) -> Optional[Dict[str, Any]]:
    """Return the validated binding entry for ``session_id``, or ``None``.

    Missing, unreadable, corrupt or schema-invalid entries are all
    ``None``; an invalid entry is logged by session id only. Call under
    ``sink_lock``.
    """
    index = _read_sidecar(sidecar_path_for(Path(sink_path)))
    bucket = index.get(_SESSION_BINDING_KEY)
    if not isinstance(bucket, dict):
        return None
    entry = bucket.get(session_id)
    if entry is None:
        return None
    if not _valid_binding_entry(entry):
        logger.warning(
            "session binding for %s is malformed and will be treated as missing",
            session_id[:12],
        )
        return None
    return dict(entry)


def record_session_binding(sink_path: Path, session_id: str, entry: Dict[str, Any]) -> bool:
    """Write (or replace) the binding entry for ``session_id`` atomically.

    Read-modify-write of the whole index, like ``update_session_state`` and
    ``record_emitted_turns``, so every other bucket survives. Returns True
    on success; logs and returns False on ``OSError`` (fail-open: the next
    process takes the recovery path). Call under ``sink_lock``.
    """
    sidecar = sidecar_path_for(Path(sink_path))
    try:
        index = _read_sidecar(sidecar)
        bucket = index.get(_SESSION_BINDING_KEY)
        if not isinstance(bucket, dict):
            bucket = {}
        bucket[session_id] = dict(entry)
        index[_SESSION_BINDING_KEY] = bucket
        _write_sidecar_atomic(sidecar, index)
        return True
    except OSError as exc:
        logger.warning("session binding update failed for %s: %s", sidecar, exc)
        return False


def build_binding_entry(
    *,
    resolution: str,
    binding: Optional[str],
    profile_content_hash: Optional[str],
    source_path: Optional[str],
    bound_by: str,
    recovered_from: Optional[str] = None,
    bound_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble a binding entry in the CP1-D schema.

    ``profile_content_hash`` ``None`` means the session is bound to NO
    profile (which is distinct from having no binding at all).
    """
    if profile_content_hash is None:
        fp = ch = snap = None
    else:
        ch = profile_content_hash
        fp = ch[:12]
        snap = snapshot_relative_path(ch)
    return {
        "schema": SESSION_BINDING_SCHEMA,
        "resolution": resolution,
        "binding": binding,
        "profile_fingerprint": fp,
        "profile_content_hash": ch,
        "snapshot": snap,
        "source_path": source_path if ch is not None else None,
        "bound_at": bound_at or _iso_now(),
        "bound_by": bound_by,
        "recovered_from": recovered_from,
    }


def _iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@dataclass(frozen=True)
class Registration:
    """What the trace's first ``AGENT_REGISTERED`` for a session says about
    its governing profile.

    ``has_provenance`` is True only when the registration payload carries
    ``profile_resolution`` (written by a v0.3.2+ runtime under a bound or
    degraded resolution). Pre-v0.3.2 registrations, and v0.3.2 default/none
    registrations, carry no provenance; recovery then compares the
    fingerprint only.
    """

    found: bool
    profile_fingerprint: Optional[str] = None
    profile_resolution: Optional[str] = None
    profile_binding: Optional[str] = None

    @property
    def has_provenance(self) -> bool:
        return self.profile_resolution is not None


def read_registration(sink_path: Path, session_id: str) -> Registration:
    """Return the first ``AGENT_REGISTERED`` for ``session_id`` in the sink.

    Mirrors the intent-rehydration reader: a substring gate, then a JSON
    parse of candidate lines, then an ``event_type`` and ``session_id``
    check (shared-file mode interleaves sessions). Never raises;
    ``Registration(found=False)`` when nothing is found or the sink cannot
    be read.
    """
    try:
        with Path(sink_path).open("r", encoding="utf-8") as handle:
            for line in handle:
                if "AGENT_REGISTERED" not in line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if event.get("event_type") != "AGENT_REGISTERED":
                    continue
                if event.get("session_id") != session_id:
                    continue
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    payload = {}
                return Registration(
                    found=True,
                    profile_fingerprint=_str_or_none(event.get("profile_fingerprint")),
                    profile_resolution=_str_or_none(payload.get("profile_resolution")),
                    profile_binding=_str_or_none(payload.get("profile_binding")),
                )
    except OSError:
        return Registration(found=False)
    return Registration(found=False)


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None
