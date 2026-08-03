"""Undeclared-intent token spend analyzer (v0.2.4).

For a given session, computes how much of the agent's compute was
attributed to reasoning turns that touched execution outside the
session's declared operational intent.

This is the first derived metric over v0.2.3's token-attribution
substrate. It is fully grounded in v0.2.3 trace fields. No schema
changes. No new event types. No probabilistic inference.

Module guarantees (load-bearing — see plan v3 §"Module shape"):

* No I/O. No file reads, no network, no logging side effects.
* No environment state read. No ``os.environ``, no time-based
  branches, no random sources.
* No input mutation. Read-only access patterns; defensive copies
  where needed.
* Byte-stable output for identical inputs. ``repr(result_a) ==
  repr(result_b)`` holds across runs.

These guarantees enable golden-trace tests, replay, and snapshot
comparison without re-validating the analyzer each time.

Algorithm — turn-window bracketing model (plan v3 §"Aggregation
algorithm"):

The wrapper schema does not allow simple "pair SCOPE_ASSERTED with
CONTEXT_SNAPSHOT by ``tool_id``" — ``ContextSnapshotPayload`` does
not carry ``tool_id``. The model implemented here avoids pairing
entirely:

1. Walk events in emit order, scoped to one session.
2. ``CONTEXT_SNAPSHOT`` events carrying populated ``llm_turn_id``
   establish (or advance) the active reasoning turn.
3. ``SCOPE_ASSERTED`` events carrying ``INTENT_MISSING`` (advisory
   flag) or ``POL-001`` (policy violation) at the **event-envelope**
   level buffer their reasons until the next turn-establishing
   ``CONTEXT_SNAPSHOT``, then attribute to that turn.
4. Per-turn token totals come from the first ``CONTEXT_SNAPSHOT``
   per ``(session_id, llm_turn_id)`` with populated token fields
   (dedupe precedence — first populated wins; conflicts increment
   ``dedupe_conflict_count``).
5. ``undeclared_tokens`` = sum of per-turn totals where the turn
   has any buffered reasons.

This model is surface-agnostic: it handles the MCP wrapper's emit
shape (``SCOPE_ASSERTED → CONTEXT_SNAPSHOT``) and the Claude Code
hook's dual-snapshot shape (``CONTEXT_SNAPSHOT pre → SCOPE_ASSERTED
→ CONTEXT_SNAPSHOT post``) uniformly.

"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants — copy of v0.2.3 enum string values rather than importing the
# enums, so the analyzer is decoupled from the schema module's import path
# and remains a pure-function module with no side-effecting imports.
# These string values are the authoritative wire-format values from
# `sentience_governor/schema/events.py`.
# ---------------------------------------------------------------------------

EVENT_TYPE_SCOPE_ASSERTED = "SCOPE_ASSERTED"
EVENT_TYPE_CONTEXT_SNAPSHOT = "CONTEXT_SNAPSHOT"
EVENT_TYPE_INTENT_DECLARED = "INTENT_DECLARED"
EVENT_TYPE_AGENT_REGISTERED = "AGENT_REGISTERED"

ADVISORY_FLAG_INTENT_MISSING = "INTENT_MISSING"
POLICY_VIOLATION_POL_001 = "POL-001"

# v0.2.5 — profile-driven advisory flags. The analyzer scans the
# trace for these and surfaces the events in derived report
# sections. The analyzer does NOT consult the operator's
# ~/.sentience/profile.yaml file (per plan §Analyzer semantics): it
# works entirely from trace contents to preserve pure-function
# guarantees and avoid drift between profile-at-capture vs
# profile-at-analysis.
ADVISORY_FLAG_HIGH_CONSEQUENCE_DETECTED = "HIGH_CONSEQUENCE_DETECTED"
ADVISORY_FLAG_TASK_BOUNDARY_CROSSED = "TASK_BOUNDARY_CROSSED"

# Status values returned in the result dict.
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_NO_TOKEN_DATA = "no_token_data"
STATUS_NO_TURNS = "no_turns"

# Threshold for malformed-event ratio that flips status from ok → partial.
_PARTIAL_MALFORMED_THRESHOLD = 0.25

# Token field names — used in dedupe precedence and per-turn aggregation.
_TOKEN_FIELDS = (
    "llm_prompt_tokens",
    "llm_completion_tokens",
    "llm_cached_read_tokens",
    "llm_cached_write_tokens",
)


# ---------------------------------------------------------------------------
# Internal types — small dataclass-like dicts kept as plain dicts so the
# byte-stable output guarantee is trivially satisfied (dicts of primitives
# serialize deterministically under repr() in Python 3.7+).
# ---------------------------------------------------------------------------


def _new_turn_data() -> Dict[str, Any]:
    """Initialize a fresh turn-data dict.

    Each entry tracks: token totals (filled when the first populated
    CONTEXT_SNAPSHOT for the turn arrives), the list of reasons (any
    INTENT_MISSING or POL-001 from buffered SCOPE_ASSERTED events),
    and the list of tool_ids that touched the turn (for the
    undeclared_turns output).
    """
    return {
        "tokens": {field: 0 for field in _TOKEN_FIELDS},
        "tokens_populated": False,
        "reasons": [],  # list of strings: "INTENT_MISSING" / "POL-001"
        "tool_ids": [],  # list of tool_ids contributing reasons
        "first_seen_index": -1,  # event index where this turn first appeared (for stable ordering)
    }


def _build_join_index(events_list: List[Dict[str, Any]]) -> Dict[str, str]:
    """Pre-pass — `tool_use_id → llm_turn_id` from token-bearing snapshots.

    v0.2.6.1 (D3): lets a SCOPE_ASSERTED reason (INTENT_MISSING / POL-001) on a
    Claude Code tool call attribute to its model turn by id rather than by event
    position (the token-bearing snapshots arrive at the end of the trace).
    Empty for traces without such snapshots → positional path unchanged.
    """
    reverse_index: Dict[str, str] = {}
    for event in events_list:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") != EVENT_TYPE_CONTEXT_SNAPSHOT:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        turn_id = payload.get("llm_turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        tuids = payload.get("tool_use_ids")
        if isinstance(tuids, list):
            for tid in tuids:
                if isinstance(tid, str) and tid and tid not in reverse_index:
                    reverse_index[tid] = turn_id
    return reverse_index


def _join_turn_for_event(
    event: Dict[str, Any], reverse_index: Dict[str, str]
) -> Optional[str]:
    """Return the joined turn_id for an event's payload tool_use_id, or None."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    tuid = payload.get("tool_use_id")
    if not isinstance(tuid, str) or not tuid:
        return None
    return reverse_index.get(tuid)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_undeclared_intent_spend(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute undeclared-intent token spend from a session event list.

    Args:
        events: List of governance events for a single session, in
            emit order. Typically loaded from an NDJSON trace.
            Pure function — events are not mutated.

    Returns:
        A structured dict matching the JSON output schema in plan v3.
        Status field indicates ``ok`` / ``partial`` / ``no_token_data`` /
        ``no_turns``.

    Raises:
        Nothing. Malformed events are skipped (per plan v3
        §"Malformed-trace handling"), individual extraction failures
        increment warning counters, and the analyzer always returns
        a structured result.
    """
    # Defensive: accept any iterable, materialize to list once for stable
    # iteration. Do not modify the caller's list.
    events_list = list(events) if events is not None else []
    total_event_count = len(events_list)

    # v0.2.6.1 CP4a — pre-pass: tool_use_id -> turn_id join index. Empty for
    # non-Claude-Code traces, so the positional bracketing below is unchanged.
    reverse_index = _build_join_index(events_list)

    # Walk state.
    current_turn_id: Optional[str] = None
    pending_scope_reasons: List[Dict[str, str]] = []  # [{reason, tool_id}, ...]
    turn_data: Dict[str, Dict[str, Any]] = {}
    session_id: Optional[str] = None
    session_has_declared_intent = False

    # v0.2.5 — profile metadata from AGENT_REGISTERED. All three
    # remain None when the session was not governed by a profile;
    # the renderer omits the Profile section in that case (regression
    # guard: v0.2.4 traces produce byte-identical CLI output).
    profile_fingerprint: Optional[str] = None
    profile_loaded: Optional[bool] = None
    profile_schema_version: Optional[int] = None

    # v0.2.5 — derived event lists from profile-driven advisory flags.
    # Collected in emit order; turn_id and tool_id are sourced from
    # the SCOPE_ASSERTED event that fired the flag.
    high_consequence_events: List[Dict[str, Any]] = []
    task_boundary_events: List[Dict[str, Any]] = []

    # Warning counters.
    unpaired_event_count = 0
    untokened_pair_count = 0
    dedupe_conflict_count = 0
    malformed_event_count = 0
    warnings: List[Dict[str, Any]] = []

    # F21 (v0.2.9) — session-wide tool-call counts. Every SCOPE_ASSERTED
    # is one tool call. Count by operation_type (the four op classes,
    # mirroring the IR-4 token-class breakdown shape) and by tool_id (for
    # the tool-level view). Independent of token data and undeclared intent.
    tool_calls_total = 0
    tool_calls_by_op = {"execute": 0, "read": 0, "write": 0, "delete": 0}
    tool_calls_by_tool: Dict[str, int] = {}

    # IR-3 (v0.2.9) — per-turn tool-call map: turn_id -> ordered list of the
    # tool_ids that fired on that turn (ALL tool calls, not just
    # undeclared-touching ones). Drives the measured A1/A2 attribution
    # (tokens on turns involving tool calls). Built via the tool_use_id ->
    # llm_turn_id join, so it is empty for traces without token-bearing
    # snapshots — A1/A2 then report zero, never an inferred split.
    turn_tool_ids: Dict[str, List[str]] = {}

    for event_index, event in enumerate(events_list):
        # Malformed-event guard. Skip + count + warn; never raise.
        if not isinstance(event, dict):
            malformed_event_count += 1
            warnings.append(
                {"code": "malformed_event", "event_index": event_index,
                 "detail": "event is not a dict"}
            )
            continue

        event_type = event.get("event_type")
        if not isinstance(event_type, str):
            malformed_event_count += 1
            warnings.append(
                {"code": "malformed_event", "event_index": event_index,
                 "detail": "event_type missing or non-string"}
            )
            continue

        # Capture session_id from the first event that has one.
        if session_id is None:
            sid = event.get("session_id")
            if isinstance(sid, str):
                session_id = sid

        # ------------------------------------------------------------------
        # AGENT_REGISTERED — capture v0.2.5 profile metadata. Trace
        # is the authoritative source (analyzer NEVER reads the
        # operator's ~/.sentience/profile.yaml). All three fields are
        # defensively typed-guarded; malformed values are ignored
        # rather than raising.
        # ------------------------------------------------------------------
        if event_type == EVENT_TYPE_AGENT_REGISTERED:
            # Envelope-level fingerprint — present on every event in a
            # profile-loaded session, but AGENT_REGISTERED is where
            # we pin it (it's first in emit order).
            fp = event.get("profile_fingerprint")
            if isinstance(fp, str) and fp:
                profile_fingerprint = fp
            payload = event.get("payload")
            if isinstance(payload, dict):
                pl = payload.get("profile_loaded")
                if isinstance(pl, bool):
                    profile_loaded = pl
                psv = payload.get("profile_schema_version")
                if isinstance(psv, int) and not isinstance(psv, bool):
                    profile_schema_version = psv
            continue

        # ------------------------------------------------------------------
        # INTENT_DECLARED — flips session_has_declared_intent to True
        # iff the payload carries a non-empty stated_objective.
        # ------------------------------------------------------------------
        if event_type == EVENT_TYPE_INTENT_DECLARED:
            payload = event.get("payload")
            if isinstance(payload, dict):
                stated = payload.get("stated_objective")
                if isinstance(stated, str) and stated.strip():
                    session_has_declared_intent = True
            continue

        # ------------------------------------------------------------------
        # SCOPE_ASSERTED — collect undeclared-touching reasons into the
        # pending buffer. Reasons attach to the next turn-establishing
        # CONTEXT_SNAPSHOT (or count as unpaired at session end).
        #
        # CRITICAL: advisory_flags and policy_violations are at the
        # event-envelope level (NOT inside payload). Plan v3 §Findings.
        # ------------------------------------------------------------------
        if event_type == EVENT_TYPE_SCOPE_ASSERTED:
            advisory_flags = event.get("advisory_flags") or []
            policy_violations = event.get("policy_violations") or []

            # Defensive — these should be lists of strings per schema.
            if not isinstance(advisory_flags, list):
                advisory_flags = []
            if not isinstance(policy_violations, list):
                policy_violations = []

            payload = event.get("payload")
            tool_id = ""
            operation_type = ""
            if isinstance(payload, dict):
                t = payload.get("tool_id")
                if isinstance(t, str):
                    tool_id = t
                op = payload.get("operation_type")
                if isinstance(op, str):
                    operation_type = op

            # F21 — count this tool call (session-wide; every SCOPE_ASSERTED
            # is one tool call, regardless of token data or undeclared intent).
            tool_calls_total += 1
            op_key = operation_type.lower()
            if op_key in tool_calls_by_op:
                tool_calls_by_op[op_key] += 1
            if tool_id:
                tool_calls_by_tool[tool_id] = tool_calls_by_tool.get(tool_id, 0) + 1

            reasons_here: List[str] = []
            if ADVISORY_FLAG_INTENT_MISSING in advisory_flags:
                reasons_here.append(ADVISORY_FLAG_INTENT_MISSING)
            if POLICY_VIOLATION_POL_001 in policy_violations:
                reasons_here.append(POLICY_VIOLATION_POL_001)

            # IR-3 (v0.2.9) — join EVERY tool call to its model turn (by id),
            # independent of reasons, to record which tools fired on each
            # token-bearing turn. The reasons path below is unchanged: it
            # still only attributes when reasons_here, so existing fields are
            # byte-identical (_join_turn_for_event is a pure lookup).
            joined_turn = _join_turn_for_event(event, reverse_index)
            if joined_turn is not None and tool_id:
                _turn_tools = turn_tool_ids.setdefault(joined_turn, [])
                if tool_id not in _turn_tools:
                    _turn_tools.append(tool_id)

            # v0.2.6.1 CP4a — if this tool call joins to a model turn by
            # tool_use_id, attribute its reasons directly (D3, by id not
            # position); otherwise buffer for the positional path. The join
            # is inert (None) for traces without token-bearing snapshots, so
            # existing behavior is byte-identical.
            join_turn = joined_turn if reasons_here else None
            if join_turn is not None:
                if join_turn not in turn_data:
                    turn_data[join_turn] = _new_turn_data()
                    turn_data[join_turn]["first_seen_index"] = event_index
                td_join = turn_data[join_turn]
                for r in reasons_here:
                    if r not in td_join["reasons"]:
                        td_join["reasons"].append(r)
                if tool_id and tool_id not in td_join["tool_ids"]:
                    td_join["tool_ids"].append(tool_id)
            else:
                for r in reasons_here:
                    pending_scope_reasons.append({"reason": r, "tool_id": tool_id})

            # v0.2.5 — collect profile-driven advisory flag events.
            # Each entry captures the active turn (or None if no
            # CONTEXT_SNAPSHOT has established one yet) and the
            # tool_id from this SCOPE_ASSERTED. Stored in emit order
            # so renderers can present chronological lists.
            if ADVISORY_FLAG_HIGH_CONSEQUENCE_DETECTED in advisory_flags:
                high_consequence_events.append({
                    "turn_id": current_turn_id,
                    "tool_id": tool_id,
                    "event_index": event_index,
                })
            if ADVISORY_FLAG_TASK_BOUNDARY_CROSSED in advisory_flags:
                task_boundary_events.append({
                    "turn_id": current_turn_id,
                    "tool_id": tool_id,
                    "event_index": event_index,
                })
            continue

        # ------------------------------------------------------------------
        # CONTEXT_SNAPSHOT — may or may not carry llm_turn_id and tokens.
        # If llm_turn_id is populated, this snapshot establishes (or
        # advances) the active turn AND consumes any pending reasons.
        # If llm_turn_id is NOT populated, the snapshot is transparent
        # to the bracketing model (does not establish a turn, does not
        # clear the pending buffer).
        # ------------------------------------------------------------------
        if event_type == EVENT_TYPE_CONTEXT_SNAPSHOT:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                # Malformed — skip. Don't count as malformed_event because
                # the event_type was valid; just no useful payload to read.
                continue

            turn_id = payload.get("llm_turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                # Non-Track-2 CONTEXT_SNAPSHOT (no turn id). Transparent
                # to the bracketing model. But: if it carries populated
                # tokens WITHOUT a turn id, that's an integrator
                # misconfiguration; flag it.
                if _has_populated_tokens(payload):
                    untokened_pair_count += 1
                    warnings.append(
                        {"code": "untokened_pair", "event_index": event_index,
                         "detail": "CONTEXT_SNAPSHOT carried populated tokens "
                                   "without llm_turn_id"}
                    )
                continue

            # Establish or re-encounter this turn.
            if turn_id not in turn_data:
                turn_data[turn_id] = _new_turn_data()
                turn_data[turn_id]["first_seen_index"] = event_index

            # Consume any pending scope reasons into this turn.
            if pending_scope_reasons:
                td = turn_data[turn_id]
                for entry in pending_scope_reasons:
                    if entry["reason"] not in td["reasons"]:
                        td["reasons"].append(entry["reason"])
                    if entry["tool_id"] and entry["tool_id"] not in td["tool_ids"]:
                        td["tool_ids"].append(entry["tool_id"])
                pending_scope_reasons = []

            # Apply token dedupe precedence.
            if _has_populated_tokens(payload):
                td = turn_data[turn_id]
                if not td["tokens_populated"]:
                    # First populated CONTEXT_SNAPSHOT for this turn.
                    for field in _TOKEN_FIELDS:
                        v = payload.get(field)
                        if isinstance(v, int) and v >= 0:
                            td["tokens"][field] = v
                    td["tokens_populated"] = True
                else:
                    # Already have tokens for this turn; check for conflict.
                    incoming = {}
                    for field in _TOKEN_FIELDS:
                        v = payload.get(field)
                        if isinstance(v, int) and v >= 0:
                            incoming[field] = v
                    # Conflict iff any field disagrees with what we already have.
                    conflict = any(
                        incoming.get(field) is not None
                        and incoming[field] != td["tokens"][field]
                        for field in _TOKEN_FIELDS
                    )
                    if conflict:
                        dedupe_conflict_count += 1
                        warnings.append(
                            {"code": "dedupe_conflict", "event_index": event_index,
                             "detail": f"conflicting tokens for turn {turn_id[:12]}"}
                        )

            current_turn_id = turn_id
            continue

        # ------------------------------------------------------------------
        # All other event types (MEMORY_WRITE_ATTEMPT, GOVERNANCE_ERROR)
        # — skip for turn tracking. AGENT_REGISTERED is handled above
        # for v0.2.5 profile metadata extraction.
        # ------------------------------------------------------------------
        continue

    # ---------------------------------------------------------------------
    # Post-walk: any remaining pending_scope_reasons are unpaired.
    # ---------------------------------------------------------------------
    if pending_scope_reasons:
        unpaired_event_count += len(pending_scope_reasons)
        for entry in pending_scope_reasons:
            warnings.append(
                {"code": "unpaired_scope_asserted", "event_index": -1,
                 "detail": f"reason {entry['reason']} from tool "
                           f"{entry['tool_id']!r} had no following "
                           f"turn-establishing CONTEXT_SNAPSHOT"}
            )

    # ---------------------------------------------------------------------
    # Status determination + aggregation.
    # ---------------------------------------------------------------------

    # F21 — session-wide tool-call counts, available on every return path
    # (the event loop above is complete before any _build_result below).
    tool_call_counts = {
        "total": tool_calls_total,
        "by_operation": tool_calls_by_op,
        "by_tool": tool_calls_by_tool,
    }

    # No CONTEXT_SNAPSHOTs with llm_turn_id at all → no_token_data.
    if not turn_data:
        return _build_result(
            session_id=session_id,
            status=STATUS_NO_TOKEN_DATA,
            session_has_declared_intent=session_has_declared_intent,
            turn_data={},
            unpaired_event_count=unpaired_event_count,
            untokened_pair_count=untokened_pair_count,
            dedupe_conflict_count=dedupe_conflict_count,
            malformed_event_count=malformed_event_count,
            warnings=warnings,
            total_event_count=total_event_count,
            profile_fingerprint=profile_fingerprint,
            profile_loaded=profile_loaded,
            profile_schema_version=profile_schema_version,
            high_consequence_events=high_consequence_events,
            task_boundary_events=task_boundary_events,
            tool_call_counts=tool_call_counts,
            turn_tool_ids=turn_tool_ids,
        )

    # Turns established but no populated tokens anywhere → no_turns.
    any_populated = any(td["tokens_populated"] for td in turn_data.values())
    if not any_populated:
        return _build_result(
            session_id=session_id,
            status=STATUS_NO_TURNS,
            session_has_declared_intent=session_has_declared_intent,
            turn_data=turn_data,
            unpaired_event_count=unpaired_event_count,
            untokened_pair_count=untokened_pair_count,
            dedupe_conflict_count=dedupe_conflict_count,
            malformed_event_count=malformed_event_count,
            warnings=warnings,
            total_event_count=total_event_count,
            profile_fingerprint=profile_fingerprint,
            profile_loaded=profile_loaded,
            profile_schema_version=profile_schema_version,
            high_consequence_events=high_consequence_events,
            task_boundary_events=task_boundary_events,
            tool_call_counts=tool_call_counts,
            turn_tool_ids=turn_tool_ids,
        )

    # Status partial fires for any of the four counter conditions (per
    # plan v3 status definitions — "partial: analysis completed but
    # warnings accumulated (unpaired events, untokened pairs, dedupe
    # conflicts, or malformed events)"). The malformed >25% threshold is
    # still surfaced as the bigger structural concern; below that, any
    # single counter still flips to partial.
    status = STATUS_OK
    if total_event_count > 0:
        ratio = malformed_event_count / total_event_count
        if ratio > _PARTIAL_MALFORMED_THRESHOLD:
            status = STATUS_PARTIAL

    if status == STATUS_OK and (
        unpaired_event_count > 0
        or untokened_pair_count > 0
        or dedupe_conflict_count > 0
        or malformed_event_count > 0
    ):
        status = STATUS_PARTIAL

    return _build_result(
        session_id=session_id,
        status=status,
        session_has_declared_intent=session_has_declared_intent,
        turn_data=turn_data,
        unpaired_event_count=unpaired_event_count,
        untokened_pair_count=untokened_pair_count,
        dedupe_conflict_count=dedupe_conflict_count,
        malformed_event_count=malformed_event_count,
        warnings=warnings,
        total_event_count=total_event_count,
        profile_fingerprint=profile_fingerprint,
        profile_loaded=profile_loaded,
        profile_schema_version=profile_schema_version,
        high_consequence_events=high_consequence_events,
        task_boundary_events=task_boundary_events,
        tool_call_counts=tool_call_counts,
        turn_tool_ids=turn_tool_ids,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_populated_tokens(payload: Dict[str, Any]) -> bool:
    """True iff at least one of the four token fields is populated with
    a non-negative int.

    Treats negative values, non-int types, and missing fields as
    unpopulated.
    """
    for field in _TOKEN_FIELDS:
        v = payload.get(field)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return True
    return False


def _build_result(
    *,
    session_id: Optional[str],
    status: str,
    session_has_declared_intent: bool,
    turn_data: Dict[str, Dict[str, Any]],
    unpaired_event_count: int,
    untokened_pair_count: int,
    dedupe_conflict_count: int,
    malformed_event_count: int,
    warnings: List[Dict[str, Any]],
    total_event_count: int,
    profile_fingerprint: Optional[str] = None,
    profile_loaded: Optional[bool] = None,
    profile_schema_version: Optional[int] = None,
    high_consequence_events: Optional[List[Dict[str, Any]]] = None,
    task_boundary_events: Optional[List[Dict[str, Any]]] = None,
    tool_call_counts: Optional[Dict[str, Any]] = None,
    turn_tool_ids: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Assemble the final structured result dict.

    Order of keys is fixed for byte-stable output (Python 3.7+ dicts
    preserve insertion order, so building keys in a fixed order
    guarantees identical repr() across runs).

    v0.2.5 profile fields default to None / empty list so v0.2.4
    callers (and v0.2.4 traces processed by the v0.2.5 analyzer)
    produce dicts with no behavioral change in pre-existing fields.
    """
    # Aggregate per-turn totals.
    total_tokens = 0
    undeclared_tokens = 0
    declared_tokens = 0
    undeclared_turn_count = 0
    total_turn_count = len(turn_data)

    # Per-turn entries for the output, sorted by first_seen_index for
    # stable ordering.
    sorted_turns = sorted(
        turn_data.items(),
        key=lambda kv: (kv[1]["first_seen_index"], kv[0]),
    )

    undeclared_turns: List[Dict[str, Any]] = []
    # IR-4 (v0.2.8.1): accumulate per-class totals so the pulse can surface
    # the four token classes (prompt / completion / cached read / cached write).
    # These sum to total_tokens — pure presentation, no new capture.
    token_class_totals = {field: 0 for field in _TOKEN_FIELDS}
    # IR-3 (v0.2.9) — measured tool-token attribution accumulators.
    # A1: tokens on turns that fired >=1 tool call. A2: per-tool
    # full-turn-credit (NON-ADDITIVE — a turn involving N tools credits all
    # N with the full turn total).
    ttids = turn_tool_ids or {}
    a1_tokens = 0
    a2_by_tool_tokens: Dict[str, int] = {}
    a2_by_tool_turns: Dict[str, int] = {}
    for turn_id, td in sorted_turns:
        for field in _TOKEN_FIELDS:
            token_class_totals[field] += int(td["tokens"][field] or 0)
        turn_total = sum(td["tokens"][field] for field in _TOKEN_FIELDS)
        total_tokens += turn_total
        tools_on_turn = ttids.get(turn_id) or []
        if tools_on_turn:
            a1_tokens += turn_total
            for _t in tools_on_turn:
                a2_by_tool_tokens[_t] = a2_by_tool_tokens.get(_t, 0) + turn_total
                a2_by_tool_turns[_t] = a2_by_tool_turns.get(_t, 0) + 1
        if td["reasons"]:
            undeclared_tokens += turn_total
            undeclared_turn_count += 1
            undeclared_turns.append({
                "turn_id": turn_id,
                "tokens": turn_total,
                "reasons": list(td["reasons"]),
                "tool_ids": list(td["tool_ids"]),
            })
        else:
            declared_tokens += turn_total

    # Ratio + percent calculations. Avoid division by zero.
    if total_tokens > 0:
        undeclared_ratio = undeclared_tokens / total_tokens
        undeclared_percent = round(undeclared_ratio * 100, 1)
    else:
        undeclared_ratio = 0.0
        undeclared_percent = 0.0

    # F21 (v0.2.9) — tool-call counts as a first-class block, stable shape
    # (four op classes + total + per-tool). by_operation sums to total for
    # well-formed traces (operation_type is a validated enum upstream).
    tcc = tool_call_counts or {}
    tcc_by_op = tcc.get("by_operation") or {}
    tool_calls_block = {
        "total": int(tcc.get("total", 0) or 0),
        "by_operation": {
            "execute": int(tcc_by_op.get("execute", 0) or 0),
            "read": int(tcc_by_op.get("read", 0) or 0),
            "write": int(tcc_by_op.get("write", 0) or 0),
            "delete": int(tcc_by_op.get("delete", 0) or 0),
        },
        "by_tool": dict(tcc.get("by_tool") or {}),
    }

    # IR-3 (v0.2.9) — measured tool-token attribution. A1 is the headline
    # (tokens on turns that fired >=1 tool call). A2 is the secondary,
    # per-tool full-turn-credit view and is NON-ADDITIVE. Per-tool token
    # PRECISION is not measurable (the model meters usage per turn, not per
    # tool), so these are turn-attributed measurements — never per-tool spend.
    a1_percent = (
        round(a1_tokens / total_tokens * 100, 1) if total_tokens > 0 else 0.0
    )
    a2_by_tool = [
        {
            "tool_id": _t,
            "tokens": a2_by_tool_tokens[_t],
            "turn_count": a2_by_tool_turns[_t],
        }
        for _t in sorted(
            a2_by_tool_tokens, key=lambda k: (-a2_by_tool_tokens[k], k)
        )
    ]
    tool_token_attribution = {
        "tokens_on_turns_with_tool_calls": a1_tokens,
        "total_tokens": total_tokens,
        "percent_of_total": a1_percent,
        "by_tool": a2_by_tool,
        "by_tool_is_non_additive": True,
    }

    return {
        "session_id": session_id or "",
        "status": status,
        "session_has_declared_intent": session_has_declared_intent,
        "total_tokens": total_tokens,
        # IR-4 (v0.2.8.1): per-class breakdown (sums to total_tokens).
        "token_breakdown": {
            "prompt": token_class_totals["llm_prompt_tokens"],
            "completion": token_class_totals["llm_completion_tokens"],
            "cached_read": token_class_totals["llm_cached_read_tokens"],
            "cached_write": token_class_totals["llm_cached_write_tokens"],
        },
        # F21 (v0.2.9): session-wide tool-call counts (op classes + per-tool).
        "tool_calls": tool_calls_block,
        # IR-3 (v0.2.9): measured tool-token attribution (A1 headline + A2
        # per-tool full-turn-credit, non-additive). Never per-tool spend.
        "tool_token_attribution": tool_token_attribution,
        "undeclared_tokens": undeclared_tokens,
        "declared_tokens": declared_tokens,
        "undeclared_ratio": undeclared_ratio,
        "undeclared_percent": undeclared_percent,
        "undeclared_turn_count": undeclared_turn_count,
        "total_turn_count": total_turn_count,
        "undeclared_turns": undeclared_turns,
        "warnings": warnings,
        "unpaired_event_count": unpaired_event_count,
        "untokened_pair_count": untokened_pair_count,
        "dedupe_conflict_count": dedupe_conflict_count,
        "malformed_event_count": malformed_event_count,
        # v0.2.5 — profile-aware fields. Always present in the result
        # dict for forward consistency, but None / empty list when no
        # profile metadata was found in the trace (preserves v0.2.4
        # numeric behavior; only adds keys, never changes values of
        # existing keys).
        "profile_fingerprint": profile_fingerprint,
        "profile_loaded": profile_loaded,
        "profile_schema_version": profile_schema_version,
        "high_consequence_events": list(high_consequence_events or []),
        "task_boundary_events": list(task_boundary_events or []),
    }
