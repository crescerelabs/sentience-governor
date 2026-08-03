"""In-Process Cache — per-session governance state.

Lock ownership
--------------
InProcessCache owns:
  - cache-level read/write lock (_cache_lock) scoped to cache access only.

It does NOT own the per-session sequencing lock (owned by SessionManager).

Preconditions enforced
----------------------
* SCOPE_INTENT_MISMATCH cannot fire before the first INTENT_DECLARED.
* SENSITIVITY_ESCALATION cannot fire before the second CONTEXT_SNAPSHOT.
Both are guaranteed by returning None until the respective values are set.

v0.2.5 additions
----------------
* ``pol_001_already_fired`` — supports the profile ``demand_at:
  first_write`` mode (POL-001 fires once per session, not per event).
  Initialized False; set True the first time the policy evaluator
  records POL-001 for that session.
* ``last_target_system`` / ``last_target_dir`` / ``last_file_ext`` /
  ``last_operation_type`` / ``last_scope_activity_monotonic`` — used
  by task-boundary signal detection (dir_change, file_type_shift,
  time_gap, read_to_write_transition). All optional; populated by the
  evaluator the first time it sees a SCOPE_ASSERTED event per session.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# Canonical sensitivity tier ordering (lowest → highest).
SENSITIVITY_TIERS = ["public", "internal", "confidential", "pii", "restricted"]


def max_sensitivity_tier(classifications: List[str]) -> Optional[str]:
    """Return the highest sensitivity tier from a list of classification strings.

    Returns None if the list is empty (treated as below 'public').
    """
    best: Optional[int] = None
    for cls in classifications:
        cls_lower = cls.lower()
        try:
            idx = SENSITIVITY_TIERS.index(cls_lower)
            if best is None or idx > best:
                best = idx
        except ValueError:
            pass  # unknown classification — treat as below public
    if best is None:
        return None
    return SENSITIVITY_TIERS[best]


_NO_PRIOR = object()  # Sentinel: no prior snapshot exists yet


@dataclass
class SessionCacheEntry:
    """Per-session governance state (v0.2.4 core + v0.2.5 profile state)."""
    # Set once on first INTENT_DECLARED; never overwritten.
    intent_stated_objective: Optional[str] = None
    intent_scope_hint: List[str] = field(default_factory=list)
    intent_baseline_set: bool = False

    # Overwritten on every CONTEXT_SNAPSHOT.
    # None means "unclassified (below public)"; _NO_PRIOR means "no snapshot yet".
    prior_context_sensitivity_tier: object = field(default_factory=lambda: _NO_PRIOR)
    context_snapshot_count: int = 0

    # ---- v0.2.5: governance-profile-driven state ----
    # POL-001-already-fired bit. Used by demand_at: first_write to fire
    # POL-001 once per session rather than per mutating event. Other
    # demand_at modes ignore this bit.
    pol_001_already_fired: bool = False

    # Task-boundary previous-state. Updated on every SCOPE_ASSERTED that
    # the evaluator processes; consulted by signal detection on the
    # subsequent SCOPE_ASSERTED. None on the first SCOPE_ASSERTED for
    # the session (no prior baseline → no boundary).
    last_target_system: Optional[str] = None
    last_target_dir: Optional[str] = None
    last_file_ext: Optional[str] = None
    last_operation_type: Optional[str] = None
    last_scope_activity_monotonic: Optional[float] = None


class InProcessCache:
    """Dict keyed by session_id; each entry holds exactly two governance values.

    Thread safety
    -------------
    The dict is protected by a single threading.RLock.  Reads and writes both
    acquire the lock (simple approach — sessions are short, contention is low).
    """

    def __init__(self) -> None:
        self._cache: Dict[str, SessionCacheEntry] = {}
        self._cache_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def init_session(self, session_id: str) -> None:
        """Allocate a blank entry for a new session."""
        with self._cache_lock:
            self._cache[session_id] = SessionCacheEntry()

    def clear_session(self, session_id: str) -> None:
        """Remove entry on session CLOSED.  Never persisted anywhere."""
        with self._cache_lock:
            self._cache.pop(session_id, None)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def set_intent_baseline(
        self,
        session_id: str,
        stated_objective: Optional[str],
        scope_hint: List[str],
    ) -> None:
        """Record intent baseline from the first INTENT_DECLARED.  Idempotent."""
        with self._cache_lock:
            entry = self._cache.get(session_id)
            if entry is None:
                return
            if not entry.intent_baseline_set:
                entry.intent_stated_objective = stated_objective
                entry.intent_scope_hint = list(scope_hint)
                entry.intent_baseline_set = True

    def update_context_tier(
        self,
        session_id: str,
        data_classifications: List[str],
    ) -> object:
        """Store the new tier; return the *prior* value (for escalation check).

        Returns _NO_PRIOR sentinel for the first snapshot (cannot escalate).
        Returns None (or a tier string) for subsequent snapshots, where None
        means the prior snapshot had empty/unclassified classifications.
        """
        new_tier = max_sensitivity_tier(data_classifications)
        with self._cache_lock:
            entry = self._cache.get(session_id)
            if entry is None:
                return _NO_PRIOR
            prior_value = entry.prior_context_sensitivity_tier
            entry.prior_context_sensitivity_tier = new_tier
            entry.context_snapshot_count += 1
            return prior_value  # _NO_PRIOR on first call, tier/None on subsequent

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_intent_baseline(
        self, session_id: str
    ) -> Optional[SessionCacheEntry]:
        """Return entry (or None if session not found / baseline not yet set)."""
        with self._cache_lock:
            entry = self._cache.get(session_id)
            if entry is None or not entry.intent_baseline_set:
                return None
            return entry

    # ------------------------------------------------------------------
    # v0.2.5 — profile-state read/write helpers
    # ------------------------------------------------------------------

    def has_pol_001_fired(self, session_id: str) -> bool:
        """True iff POL-001 was already recorded for this session.

        Used by demand_at: first_write so the evaluator fires POL-001
        once per session rather than once per mutating event. Returns
        False for unknown sessions (defensive — should not happen in
        practice).
        """
        with self._cache_lock:
            entry = self._cache.get(session_id)
            return bool(entry and entry.pol_001_already_fired)

    def mark_pol_001_fired(self, session_id: str) -> None:
        """Record that POL-001 fired for this session. Idempotent."""
        with self._cache_lock:
            entry = self._cache.get(session_id)
            if entry is not None:
                entry.pol_001_already_fired = True

    def get_task_boundary_state(
        self, session_id: str
    ) -> Optional[SessionCacheEntry]:
        """Return the cache entry (callers read last_* fields directly).

        Returns None for unknown sessions. Unlike get_intent_baseline,
        this does NOT gate on a flag — task-boundary state is built up
        from the very first SCOPE_ASSERTED, so callers must check the
        individual last_* fields for None to detect the first event.
        """
        with self._cache_lock:
            return self._cache.get(session_id)

    def update_task_boundary_state(
        self,
        session_id: str,
        *,
        target_system: Optional[str],
        target_dir: Optional[str],
        file_ext: Optional[str],
        operation_type: Optional[str],
        activity_monotonic: Optional[float],
    ) -> None:
        """Overwrite the last_* task-boundary fields for this session.

        Called by the evaluator AFTER it has consulted the prior values
        for boundary detection on the current event. Idempotent for
        repeated calls with the same args.
        """
        with self._cache_lock:
            entry = self._cache.get(session_id)
            if entry is None:
                return
            entry.last_target_system = target_system
            entry.last_target_dir = target_dir
            entry.last_file_ext = file_ext
            entry.last_operation_type = operation_type
            entry.last_scope_activity_monotonic = activity_monotonic
