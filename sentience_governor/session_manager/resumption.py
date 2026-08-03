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

import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

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
