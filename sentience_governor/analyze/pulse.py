"""compute_pulse — v0.2.6 composition analyzer.

Pulse is the v0.2.6 adoption surface. It composes the v0.2.4
:func:`compute_undeclared_intent_spend` analyzer with the v0.2.6 CP1
:func:`compute_policy_violation_burn_rate` analyzer, plus an
advisory-flag-occurrence summary, and emits a single result dict that
the v0.2.6 CP5 renderers turn into the operator-facing
``sentience pulse`` output.

Purity contract (per plan v3.6 §CP4):

* :func:`compute_pulse` is a **pure function**. No disk reads. No
  env-var reads. No filesystem state. No mutation of the caller's
  ``events`` list. Repeated calls with identical input yield
  byte-identical output (deepcopy-friendly).

* Sync-prompt eligibility is **NOT** computed here. The result dict
  ships a default ``sync_prompt = {"show": False, "reason":
  "uninitialized"}``. The CP6 CLI handler
  (``sentience_governor.cli.ux.run_pulse``) reads sync state and
  ``SENTIENCE_NO_SYNC_PROMPT``, then overwrites the field before
  passing the result to the renderer. Keeping disk/env IO out of the
  analyzer is what makes pulse replay-stable across machines and
  testable without filesystem fixtures.

Status merge model (per plan v3.6 §"Status merge logic —
category-normalized"):

* Each sub-analyzer's raw status is normalized to one of the five
  categories ``usable_ok`` / ``usable_clean`` / ``limited_signal`` /
  ``partial`` / ``no_signal`` via :func:`_normalize_status`. New
  analyzers in v0.2.7+ register a single new mapping here; the
  merge table downstream is unchanged.
* The normalized categories are merged via :func:`_merge_status` to
  one of the four pulse-level statuses ``ok`` / ``limited`` /
  ``no_signal`` / ``partial`` (right-hand column of the spec's merge
  table). ``no_signal`` at the pulse level is distinct from the
  reserved-for-future-use ``no_signal`` normalized category — see
  the plan's naming-disambiguation paragraph.

CP4 verify-step findings (recorded inline so future readers see the
assumptions):

1. ``AdvisoryFlag`` enum (``sentience_governor/schema/events.py``)
   has **10** members; the plan's example
   ``advisory_flag_summary`` dict enumerates 7. This module counts
   ALL ten so no flag is silently dropped; the example dict is
   illustrative rather than exhaustive.

2. ``session_duration_seconds`` is computed as ``max(timestamp_utc)
   - min(timestamp_utc)`` across all events with parseable
   ISO-8601 ``timestamp_utc`` strings; there is no SessionStart /
   SessionEnd marker on the trace today. Returns ``0`` when fewer
   than two timestamps are parseable (e.g. empty event list).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sentience_governor.analyze.policy_violation_burn_rate import (
    compute_policy_violation_burn_rate,
)
from sentience_governor.analyze.undeclared_intent import (
    compute_undeclared_intent_spend,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = 1
_ANALYZER_NAME = "pulse"
_ANALYZER_VERSION = "0.2.6"

# All AdvisoryFlag enum members shipped in the v0.2.5 schema. The
# summary dict is initialized with zero counts for every key so the
# renderer can rely on a stable key set and downstream code can index
# any flag without KeyError. Kept in enum-declaration order for
# stability across runs.
_ADVISORY_FLAG_KEYS: Tuple[str, ...] = (
    "AGENT_UNREGISTERED",
    "INTENT_MISSING",
    "SCOPE_OPERATION_UNEXPECTED",
    "SCOPE_INTENT_MISMATCH",
    "CONTEXT_UNCLASSIFIED",
    "SENSITIVITY_ESCALATION",
    "MEMORY_WRITE_UNCLASSIFIED",
    "MEMORY_WRITE_CANDIDATE",
    "TASK_BOUNDARY_CROSSED",
    "HIGH_CONSEQUENCE_DETECTED",
)

# Per-analyzer status → normalized-category mapping. New analyzers in
# v0.2.7+ add a single entry here; the merge table in
# :func:`_merge_status` does not change. Any raw status not present
# in the per-analyzer table falls through to ``limited_signal`` (a
# conservative bias — if pulse doesn't recognize the status, it
# refuses to treat it as usable signal).
_STATUS_CATEGORY_BY_ANALYZER: Dict[str, Dict[str, str]] = {
    "undeclared_intent": {
        "ok": "usable_ok",
        "partial": "partial",
        "no_token_data": "limited_signal",
        "no_turns": "limited_signal",
    },
    "policy_violation_burn_rate": {
        "ok": "usable_ok",
        "no_violations": "usable_clean",
        "partial": "partial",
        "no_token_data": "limited_signal",
        "no_turns": "limited_signal",
    },
}

# Default sync_prompt block. The CP6 CLI handler overwrites this with
# real eligibility once it reads ~/.sentience/first-run.json (launch-list state) and
# the SENTIENCE_NO_SYNC_PROMPT env var. Programmatic callers of
# compute_pulse see the uninitialized default; the renderer treats
# show=false as "do not display footer."
_DEFAULT_SYNC_PROMPT: Dict[str, Any] = {
    "show": False,
    "reason": "uninitialized",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_event_iter(events: Iterable[Any]) -> List[Dict[str, Any]]:
    """Filter the input iterable to dict-typed events only.

    Mirrors the defensive walker behavior shared with the v0.2.4 and
    v0.2.6 CP1 analyzers: malformed (non-dict) entries are skipped
    silently here, since the downstream analyzers each emit their own
    warnings for malformed events.
    """

    return [e for e in events if isinstance(e, dict)]


def _summarize_advisory_flags(events: Iterable[Any]) -> Dict[str, int]:
    """Count occurrences of each :class:`AdvisoryFlag` member across
    all events in the trace.

    Returns a dict keyed by every member of
    :data:`_ADVISORY_FLAG_KEYS` (initialized to ``0`` so callers can
    index any flag without KeyError). Unknown flag strings are
    ignored — keeping the key set stable is a renderer / forward-
    compat contract.
    """

    counts = {key: 0 for key in _ADVISORY_FLAG_KEYS}
    for event in _safe_event_iter(events):
        flags = event.get("advisory_flags")
        if not isinstance(flags, list):
            continue
        for flag in flags:
            if isinstance(flag, str) and flag in counts:
                counts[flag] += 1
    return counts


def _normalize_status(raw_status: Optional[str], analyzer_name: str) -> str:
    """Map a sub-analyzer's raw status string to one of the five
    normalized categories per the plan's §"Status merge logic"
    spec.

    Unknown analyzers or unknown raw statuses normalize to
    ``limited_signal`` (conservative: pulse refuses to treat an
    unrecognized status as usable signal). Returns one of:
    ``usable_ok`` / ``usable_clean`` / ``limited_signal`` /
    ``partial`` / ``no_signal``.
    """

    table = _STATUS_CATEGORY_BY_ANALYZER.get(analyzer_name, {})
    if not isinstance(raw_status, str):
        return "limited_signal"
    return table.get(raw_status, "limited_signal")


def _merge_status(categories: Iterable[str]) -> str:
    """Merge an iterable of normalized categories into a single
    pulse-level status per the spec's merge table.

    Returns one of ``ok`` / ``limited`` / ``no_signal`` /
    ``partial``. Operates entirely on category strings — knows
    nothing about which analyzer contributed which category.
    """

    cats = list(categories)
    if not cats:
        # Defensive: no sub-analyzers contributed. Treat as no_signal
        # at the pulse level so the renderer can prepend the
        # appropriate notice.
        return "no_signal"
    if any(c == "partial" for c in cats):
        # Conservative — any partial analyzer drags the merged
        # status to partial. Matches the spec's "any + partial" row.
        return "partial"
    if all(c in ("limited_signal", "no_signal") for c in cats):
        # Spec: limited_signal + limited_signal → no_signal. Also
        # covers the reserved-for-future no_signal normalized
        # category if a sub-analyzer ever emits it.
        return "no_signal"
    if any(c == "limited_signal" for c in cats):
        # At least one usable_* AND at least one limited_signal →
        # limited (partial picture; renderer prepends "N of M
        # analyzers had usable data" notice).
        return "limited"
    # Remaining cases are all combinations of usable_ok and
    # usable_clean — every one of those merges to pulse status ok per
    # the spec's first three rows.
    return "ok"


def _count_turns(events: Iterable[Any]) -> int:
    """Count CONTEXT_SNAPSHOT events with a populated ``llm_turn_id``
    — the established turn-window bracketing model from v0.2.4 / CP1.
    """

    count = 0
    for event in _safe_event_iter(events):
        if event.get("event_type") != "CONTEXT_SNAPSHOT":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("llm_turn_id"):
            count += 1
    return count


def _session_duration_seconds(events: Iterable[Any]) -> int:
    """Derive session duration from ``timestamp_utc`` on events.

    Returns ``int((max - min).total_seconds())`` across all events
    with parseable ISO-8601 ``timestamp_utc`` strings. Returns ``0``
    if fewer than two timestamps parse cleanly (e.g. empty event
    list, single-event trace, or all timestamps malformed).
    """

    timestamps: List[datetime] = []
    for event in _safe_event_iter(events):
        ts = event.get("timestamp_utc")
        if not isinstance(ts, str):
            continue
        try:
            # Tolerate trailing 'Z' that Python <3.11 rejects.
            normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
            timestamps.append(datetime.fromisoformat(normalized))
        except ValueError:
            continue
    if len(timestamps) < 2:
        return 0
    delta = max(timestamps) - min(timestamps)
    return int(delta.total_seconds())


def _build_session_summary(events: Iterable[Any]) -> Dict[str, int]:
    """Compose the ``session_summary`` sub-dict.

    Three fields: ``total_events`` (count of dict-typed entries —
    malformed JSON lines are excluded), ``total_turns``
    (CONTEXT_SNAPSHOT-with-``llm_turn_id`` count), and
    ``session_duration_seconds`` (event-timestamp span).
    """

    safe = _safe_event_iter(events)
    return {
        "total_events": len(safe),
        "total_turns": _count_turns(safe),
        "session_duration_seconds": _session_duration_seconds(safe),
    }


def _select_profile_metadata(
    undeclared: Dict[str, Any],
    burn_rate: Dict[str, Any],
) -> Tuple[Optional[str], Optional[bool], Optional[int]]:
    """Pick profile metadata from whichever sub-analyzer surfaced it.

    Both analyzers extract the same fields from the same event-
    envelope source (``profile_fingerprint`` etc. on each event), so
    they SHOULD agree. Prefer the undeclared-intent value when both
    are populated; fall back to the burn-rate value otherwise. This
    keeps pulse stable if one sub-analyzer ever reports a profile
    field the other doesn't (e.g. future divergence in extraction
    coverage).
    """

    fingerprint = (
        undeclared.get("profile_fingerprint")
        or burn_rate.get("profile_fingerprint")
    )

    loaded = undeclared.get("profile_loaded")
    if loaded is None:
        loaded = burn_rate.get("profile_loaded")

    schema = undeclared.get("profile_schema_version")
    if schema is None:
        schema = burn_rate.get("profile_schema_version")

    return fingerprint, loaded, schema


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_pulse(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compose the v0.2.6 pulse over a session's event list.

    Pure function. No IO. Calls
    :func:`compute_undeclared_intent_spend` and
    :func:`compute_policy_violation_burn_rate` against the same input
    list, summarizes advisory-flag occurrences, normalizes each
    sub-analyzer's status to a category, merges the categories to a
    pulse-level status, and returns a single result dict.

    The result dict includes a default ``sync_prompt = {"show":
    False, "reason": "uninitialized"}`` — the CP6 CLI handler
    overwrites this with real eligibility before rendering.

    See module docstring for the purity contract, status merge
    model, and verify-step findings.
    """

    # Defensive copy of the input to a list. Both downstream analyzers
    # accept any sequence; the local list lets us iterate multiple
    # times without consuming a generator and without mutating the
    # caller's object.
    safe_events: List[Dict[str, Any]] = list(events) if events is not None else []

    undeclared = compute_undeclared_intent_spend(safe_events)
    burn_rate = compute_policy_violation_burn_rate(safe_events)
    flag_summary = _summarize_advisory_flags(safe_events)

    norm_undeclared = _normalize_status(
        undeclared.get("status"), "undeclared_intent"
    )
    norm_burn_rate = _normalize_status(
        burn_rate.get("status"), "policy_violation_burn_rate"
    )
    pulse_status = _merge_status([norm_undeclared, norm_burn_rate])

    session_id = (
        undeclared.get("session_id")
        or burn_rate.get("session_id")
        or ""
    )
    fingerprint, loaded, schema = _select_profile_metadata(
        undeclared, burn_rate
    )

    return {
        "schema_version": _SCHEMA_VERSION,
        "analyzer": _ANALYZER_NAME,
        "analyzer_version": _ANALYZER_VERSION,
        "session_id": session_id,
        "status": pulse_status,
        "session_summary": _build_session_summary(safe_events),
        "undeclared_intent": undeclared,
        "policy_violations_burn_rate": burn_rate,
        "advisory_flag_summary": flag_summary,
        "sync_prompt": dict(_DEFAULT_SYNC_PROMPT),
        "profile_fingerprint": fingerprint,
        "profile_loaded": loaded,
        "profile_schema_version": schema,
    }
