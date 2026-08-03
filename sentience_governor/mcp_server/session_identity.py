"""Current-session identification (plan §7) — the load-bearing plumbing.

`declare_intent` (CP6) and `sentience_session_status` (CP4) need to know
*which* session is live. This module answers that one question, fail-closed.

Mechanism (plan §7), in order:

1. **Primary — `CLAUDE_CODE_SESSION_ID` (env).** Verified present for
   Claude-Code-spawned servers (operator `ps eww`, 2026-07-07). This is the
   *candidate* current session id, never a trusted-standalone answer.
2. **Trace cross-check (required before any write).** The candidate is
   accepted only if its trace is present and fresh and no *different* trace
   is a fresher active one; a fresher competing trace is a conflict.
3. **Fallback — bounded newest-mtime.** Only when the env id is unavailable
   AND exactly one trace is fresh within the window.
4. **Fail closed** on any uncertainty: missing / stale / ambiguous /
   conflicting. A misattributed declaration is worse than none.

Freshness is measured from the trace file's mtime (last append), which is
what actually signals activity — deliberately NOT the session-start ordering
used by the CLI's `_list_session_files`.

`CLAUDE_CODE_CHILD_SESSION` is intentionally ignored: the trace is keyed by
`CLAUDE_CODE_SESSION_ID`, so that id is authoritative; the child flag is
informational (plan §7 defined behavior).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

ENV_SESSION_ID = "CLAUDE_CODE_SESSION_ID"

#: Read/status freshness window (a UX tolerance, NOT a safety gate). Structural
#: reads like ``sentience_session_status`` can tolerate a stale-ish live trace
#: because a wrong identification there is a read-only, clearly-partial answer
#: that persists nothing. Generous enough to survive normal idle/reading gaps.
FRESHNESS_WINDOW_SECONDS = 1800  # 30 minutes — read/status default

#: Write freshness window (a SAFETY gate for ``declare_intent``). A declaration
#: stamps provenance onto a session, so a misattribution is a correctness bug,
#: not a UX blemish. A genuinely live session appends on every tool call and at
#: SessionStart, so its trace mtime is refreshed continuously; a tight window
#: rejects a stale spawn-time env (server reuse across an in-app session change)
#: whose prior-session trace has stopped being appended, and fails closed
#: instead of misattributing. See the stale-env race note in the module tests.
WRITE_FRESHNESS_WINDOW_SECONDS = 90  # seconds — declare_intent default

# Resolution statuses.
RESOLVED = "resolved"
NO_CURRENT_SESSION = "no_current_session"
NOT_YET_CAPTURED = "not_yet_captured"

# Resolution sources.
SOURCE_ENV = "env_crosschecked"
SOURCE_FALLBACK = "fallback_mtime"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class SessionResolution:
    """The answer to "which session is live?", with provenance.

    Only ``status == RESOLVED`` carries a writable ``session_id``; every other
    status is fail-closed and callers must write nothing.
    """

    status: str
    session_id: Optional[str] = None
    source: str = SOURCE_NONE
    reason: str = ""

    def writable(self) -> bool:
        return self.status == RESOLVED and self.session_id is not None


def _trace_entries(trace_dir: Path) -> List[Tuple[str, float]]:
    """``[(session_id, mtime)]`` for every trace file; unordered. Reads mtime
    directly (append-freshness), not session-start ordering."""
    if not trace_dir.exists() or not trace_dir.is_dir():
        return []
    entries: List[Tuple[str, float]] = []
    for path in trace_dir.glob("*.jsonl"):  # strict suffix: skips .jsonl.index
        if not path.is_file():
            continue
        try:
            entries.append((path.stem, path.stat().st_mtime))
        except OSError:
            continue
    return entries


def resolve_current_session(
    trace_dir: Path,
    *,
    env: Optional[Mapping[str, str]] = None,
    now: Optional[float] = None,
    freshness_window: Optional[float] = None,
) -> SessionResolution:
    """Resolve the live session id, fail-closed. See module docstring for the
    full mechanism.

    ``freshness_window`` sets how recently a trace must have been appended to
    count as "active". Callers pick it by stakes: reads/status use the loose
    :data:`FRESHNESS_WINDOW_SECONDS` (a UX tolerance) and writes use the tight
    :data:`WRITE_FRESHNESS_WINDOW_SECONDS` (a safety gate) so a stale env id
    cannot misattribute a declaration. Defaults to the read window.

    ``env`` and ``now`` are injectable for testing.
    """
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    window = (
        FRESHNESS_WINDOW_SECONDS if freshness_window is None else freshness_window
    )

    candidate = env.get(ENV_SESSION_ID) or None
    entries = _trace_entries(trace_dir)

    def _is_fresh(mtime: float) -> bool:
        return (now - mtime) <= window

    if candidate is not None:
        # Primary env read + mandatory trace cross-check.
        cand = next((e for e in entries if e[0] == candidate), None)
        if cand is None:
            # Env says a session exists but nothing is captured yet. Do NOT
            # create the trace from MCP in v1 (architect 2026-07-07).
            return SessionResolution(
                NOT_YET_CAPTURED,
                source=SOURCE_NONE,
                reason=(
                    "env session id has no trace yet; capture must happen "
                    "first (hook), so the write fails closed"
                ),
            )
        cand_mtime = cand[1]
        # A *different* trace that is both fresher than the candidate and
        # itself fresh is "clearly the active one" → conflict, fail closed.
        fresher_other = any(
            sid != candidate and mtime > cand_mtime and _is_fresh(mtime)
            for sid, mtime in entries
        )
        if fresher_other:
            return SessionResolution(
                NO_CURRENT_SESSION,
                source=SOURCE_NONE,
                reason=(
                    "env id conflicts with a fresher active trace; refusing "
                    "to trust a possibly-stale env, failing closed"
                ),
            )
        if _is_fresh(cand_mtime):
            return SessionResolution(
                RESOLVED,
                session_id=candidate,
                source=SOURCE_ENV,
                reason="env id cross-checked against a fresh live trace",
            )
        # Candidate is the only/least-stale trace but outside the window.
        return SessionResolution(
            NO_CURRENT_SESSION,
            source=SOURCE_NONE,
            reason="env id trace is stale (outside freshness window); failing closed",
        )

    # No env id (manual / non-Claude launch): bounded newest-mtime fallback.
    fresh = [(sid, mtime) for sid, mtime in entries if _is_fresh(mtime)]
    if len(fresh) == 1:
        return SessionResolution(
            RESOLVED,
            session_id=fresh[0][0],
            source=SOURCE_FALLBACK,
            reason="no env id; exactly one fresh active trace",
        )
    return SessionResolution(
        NO_CURRENT_SESSION,
        source=SOURCE_NONE,
        reason=(
            f"no env id and no single fresh trace ({len(fresh)} fresh "
            "candidates); failing closed (non-Claude launch or ambiguous)"
        ),
    )
