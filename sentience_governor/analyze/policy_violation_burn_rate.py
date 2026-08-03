"""Policy-violation burn rate analyzer (v0.2.6).

For a given session, computes how much of the agent's compute was
attributed to turns where one or more policy rules (POL-001 through
POL-005) fired. Aggregation is **per rule**: each turn's tokens are
attributed to every rule that fired during that turn.

This is the second derived metric over v0.2.3's token-attribution
substrate, sibling to `undeclared_intent`. It reads envelope-level
``policy_violations`` from every event and applies the same
turn-window bracketing model (CONTEXT_SNAPSHOT with ``llm_turn_id``
establishes the active turn).

Module guarantees (load-bearing — see v0.2.6 plan v3.6
§"Policy-violation burn rate — output shape"):

* No I/O. No file reads, no network, no logging side effects.
* No environment state read. No ``os.environ``, no time-based
  branches, no random sources.
* No input mutation. Read-only access patterns; defensive copies
  where needed.
* Byte-stable output for identical inputs. ``repr(result_a) ==
  repr(result_b)`` holds across runs.

These guarantees enable golden-trace tests, replay, and snapshot
comparison without re-validating the analyzer each time.

Algorithm — all-events walker with turn-window bracketing (per
**v3.6 verify-step finding 2026-05-28**, see plan commit `9fff30c`):

The v0.2.4 ``undeclared_intent`` walker only inspects
``policy_violations`` on ``SCOPE_ASSERTED`` events because POL-001
fires there exclusively. For burn-rate, the verify pass against
``event_builder/builder.py`` showed that EventBuilder emits
``policy_violations`` across **four distinct event types** per the
``_VIOLATION_PRIMITIVE`` mapping (builder.py lines 104-110):

* ``POL-002`` on ``AGENT_REGISTERED`` (line 514)
* ``POL-001`` on ``SCOPE_ASSERTED`` (lines 547, 560-561, 565-566)
* ``POL-003`` on ``CONTEXT_SNAPSHOT`` (line 710)
* ``POL-005`` on ``CONTEXT_SNAPSHOT`` (line 725)
* ``POL-004`` on ``MEMORY_WRITE_ATTEMPT`` (line 745)

The burn-rate walker therefore scans ALL events for envelope-level
``policy_violations``, not only ``SCOPE_ASSERTED``. Two attribution
paths are exercised:

1. **Inter-event buffered attribution** (the established v0.2.4
   pattern). Violations on ``AGENT_REGISTERED``, ``SCOPE_ASSERTED``,
   or ``MEMORY_WRITE_ATTEMPT`` are buffered until the next
   turn-establishing ``CONTEXT_SNAPSHOT`` and attribute to that turn.

2. **Same-event attribution on CONTEXT_SNAPSHOT**. If a
   ``CONTEXT_SNAPSHOT`` event carries BOTH ``llm_turn_id`` AND
   envelope-level ``policy_violations``, those violations attribute
   to **that same turn** (intra-event), not buffered for a future
   snapshot. This is the only intra-event attribution case; all
   other event types follow the inter-event buffered model.

Per-turn token totals come from the first ``CONTEXT_SNAPSHOT`` per
``(session_id, llm_turn_id)`` with populated token fields (dedupe
precedence — first populated wins; conflicts increment
``dedupe_conflict_count``).

Per-event metadata extraction (for richer per-rule attribution):

* ``AGENT_REGISTERED`` → ``agent_id`` (no ``tool_id`` available)
* ``SCOPE_ASSERTED`` → ``tool_id``, ``target_system``, ``operation_type``
* ``CONTEXT_SNAPSHOT`` → ``data_classifications``, ``provenance``
* ``MEMORY_WRITE_ATTEMPT`` → ``target_store``, ``write_classification``

Aggregation: per-turn rule sets are inverted into a per-rule turn
list. Each rule's ``token_cost`` is the sum of tokens across turns
where that rule fired. **By-rule totals are non-additive**: a turn
that fires multiple rules contributes its tokens to each rule.
``violation_associated_tokens`` deduplicates by unique
violation-firing turn so the top-level number is bounded above by
``total_tokens``.

"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Constants — copy of v0.2.3+ enum string values rather than importing the
# enums, so the analyzer is decoupled from the schema module's import path
# and remains a pure-function module with no side-effecting imports.
# These string values are the authoritative wire-format values from
# `sentience_governor/schema/events.py`.
# ---------------------------------------------------------------------------

EVENT_TYPE_AGENT_REGISTERED = "AGENT_REGISTERED"
EVENT_TYPE_INTENT_DECLARED = "INTENT_DECLARED"
EVENT_TYPE_SCOPE_ASSERTED = "SCOPE_ASSERTED"
EVENT_TYPE_CONTEXT_SNAPSHOT = "CONTEXT_SNAPSHOT"
EVENT_TYPE_MEMORY_WRITE_ATTEMPT = "MEMORY_WRITE_ATTEMPT"
EVENT_TYPE_GOVERNANCE_ERROR = "GOVERNANCE_ERROR"

# Policy violation rule descriptions. Inlined here rather than imported
# from `sentience_governor.cli.viewer._POL_FIXES` so the analyzer remains
# a pure module with no cross-package coupling. Update both in lockstep
# when the canonical rule wording changes.
_POL_DESCRIPTIONS: Dict[str, str] = {
    "POL-001": "Declare intent before executing mutating operations",
    "POL-002": "Register agent before session start",
    "POL-003": "Vendor should tag tool responses with classification metadata",
    "POL-004": "Memory writes must include classification and retention policy",
    "POL-005": "Provide explicit authorization before escalating context sensitivity",
}

# The five policy-violation rule strings we recognise. Used to bound
# warning generation for unknown rule strings without raising.
_KNOWN_POL_RULES = frozenset(_POL_DESCRIPTIONS.keys())

# Status values returned in the result dict.
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_NO_VIOLATIONS = "no_violations"
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

# Number of sample turn_ids surfaced per rule. Keeps the output bounded
# for very long sessions while still giving operators a click-through
# anchor into the trace.
_SAMPLE_TURN_LIMIT = 3

# Schema + analyzer version stamps for output dict (per plan §Result dict).
_SCHEMA_VERSION = 1
_ANALYZER_NAME = "policy_violation_burn_rate"
_ANALYZER_VERSION = "0.2.6"


# ---------------------------------------------------------------------------
# Internal types — small dataclass-like dicts kept as plain dicts so the
# byte-stable output guarantee is trivially satisfied (dicts of primitives
# serialize deterministically under repr() in Python 3.7+).
# ---------------------------------------------------------------------------


def _new_turn_data() -> Dict[str, Any]:
    """Initialize a fresh turn-data dict.

    Each entry tracks: token totals (filled when the first populated
    CONTEXT_SNAPSHOT for the turn arrives), the set of policy-violation
    rules that fired on or attribute to this turn, and metadata about
    the events that contributed those rules (for richer per-rule
    inspection downstream).
    """
    return {
        "tokens": {field: 0 for field in _TOKEN_FIELDS},
        "tokens_populated": False,
        "rules": set(),  # set of POL rule strings that fired on this turn
        "rule_metadata": [],  # list of dicts: {rule, source_event_type, ...}
        "first_seen_index": -1,
    }


def _extract_event_metadata(
    event_type: str, payload: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Extract event-type-specific context for per-rule attribution.

    Per v0.2.6 plan v3.6 CP1 verify-step finding:
      AGENT_REGISTERED       → agent_id
      SCOPE_ASSERTED         → tool_id, target_system, operation_type
      CONTEXT_SNAPSHOT       → data_classifications, provenance
      MEMORY_WRITE_ATTEMPT   → target_store, write_classification

    Defensive against missing or wrong-type payload fields; returns
    only the keys that were extractable, never raises.
    """
    if not isinstance(payload, dict):
        return {}

    if event_type == EVENT_TYPE_AGENT_REGISTERED:
        aid = payload.get("agent_id")
        return {"agent_id": aid} if isinstance(aid, str) else {}

    if event_type == EVENT_TYPE_SCOPE_ASSERTED:
        out: Dict[str, Any] = {}
        tid = payload.get("tool_id")
        if isinstance(tid, str):
            out["tool_id"] = tid
        tsys = payload.get("target_system")
        if isinstance(tsys, str):
            out["target_system"] = tsys
        op = payload.get("operation_type")
        if isinstance(op, str):
            out["operation_type"] = op
        return out

    if event_type == EVENT_TYPE_CONTEXT_SNAPSHOT:
        out = {}
        dcs = payload.get("data_classifications")
        if isinstance(dcs, list):
            out["data_classifications"] = [c for c in dcs if isinstance(c, str)]
        prov = payload.get("provenance")
        if isinstance(prov, list):
            out["provenance"] = [p for p in prov if isinstance(p, str)]
        return out

    if event_type == EVENT_TYPE_MEMORY_WRITE_ATTEMPT:
        out = {}
        ts = payload.get("target_store")
        if isinstance(ts, str):
            out["target_store"] = ts
        wc = payload.get("write_classification")
        if isinstance(wc, str):
            out["write_classification"] = wc
        return out

    return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_policy_violation_burn_rate(
    events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute policy-violation burn rate from a session event list.

    Args:
        events: List of governance events for a single session, in
            emit order. Typically loaded from an NDJSON trace.
            Pure function — events are not mutated.

    Returns:
        A structured dict matching the JSON output schema in v0.2.6
        plan v3.6 §"Policy-violation burn rate — output shape".
        Status field indicates ``ok`` / ``no_violations`` /
        ``no_token_data`` / ``no_turns`` / ``partial``.

    Raises:
        Nothing. Malformed events are skipped (per plan
        §"Malformed-trace handling"), individual extraction failures
        increment warning counters, and the analyzer always returns
        a structured result.
    """
    events_list = list(events) if events is not None else []
    total_event_count = len(events_list)

    # v0.2.6.1 CP4a — pre-pass: build the tool_use_id -> turn_id join index
    # (and per-turn provider, for CP4b ranking). Empty for traces without
    # token-bearing snapshots, so the positional path below is unchanged.
    reverse_index, turn_provider = _build_join_index(events_list)

    # v0.2.6.1 D5 — subagent (Task/Agent) burn is out of scope; flag its
    # presence so renderers can disclose the exclusion (never silently
    # present partial burn as full-session burn).
    subagent_activity_present = _detect_subagent_activity(events_list)

    # Walk state.
    pending_violation_buffer: List[Dict[str, Any]] = []
    turn_data: Dict[str, Dict[str, Any]] = {}
    session_id: Optional[str] = None

    # v0.2.5 — profile metadata extracted from AGENT_REGISTERED.
    profile_fingerprint: Optional[str] = None
    profile_loaded: Optional[bool] = None
    profile_schema_version: Optional[int] = None

    # Warning counters (mirror the v0.2.4 undeclared_intent shape).
    # NOTE: `unknown_rule_count` is derived from `warnings` after the
    # walk completes (single source of truth — see end of walk).
    unpaired_violation_count = 0
    untokened_pair_count = 0
    dedupe_conflict_count = 0
    malformed_event_count = 0
    warnings: List[Dict[str, Any]] = []

    for event_index, event in enumerate(events_list):
        # Malformed-event guard. Skip + count + warn; never raise.
        if not isinstance(event, dict):
            malformed_event_count += 1
            warnings.append(
                {
                    "code": "malformed_event",
                    "event_index": event_index,
                    "detail": "event is not a dict",
                }
            )
            continue

        event_type = event.get("event_type")
        if not isinstance(event_type, str):
            malformed_event_count += 1
            warnings.append(
                {
                    "code": "malformed_event",
                    "event_index": event_index,
                    "detail": "event_type missing or non-string",
                }
            )
            continue

        # Capture session_id from the first event that has one.
        if session_id is None:
            sid = event.get("session_id")
            if isinstance(sid, str):
                session_id = sid

        # ------------------------------------------------------------------
        # AGENT_REGISTERED — capture v0.2.5 profile metadata AND extract
        # any envelope-level policy_violations (POL-002 fires here).
        # ------------------------------------------------------------------
        if event_type == EVENT_TYPE_AGENT_REGISTERED:
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

            if not _try_join_attribute(
                event, event_index, event_type, reverse_index, turn_data, warnings
            ):
                _collect_envelope_violations(
                    event,
                    event_index,
                    event_type,
                    pending_violation_buffer,
                    warnings,
                )
            continue

        # ------------------------------------------------------------------
        # SCOPE_ASSERTED — collect any envelope-level policy_violations
        # into the buffer with tool/target metadata.
        # ------------------------------------------------------------------
        if event_type == EVENT_TYPE_SCOPE_ASSERTED:
            if not _try_join_attribute(
                event, event_index, event_type, reverse_index, turn_data, warnings
            ):
                _collect_envelope_violations(
                    event,
                    event_index,
                    event_type,
                    pending_violation_buffer,
                    warnings,
                )
            continue

        # ------------------------------------------------------------------
        # MEMORY_WRITE_ATTEMPT — collect any envelope-level
        # policy_violations into the buffer with memory-write metadata.
        # ------------------------------------------------------------------
        if event_type == EVENT_TYPE_MEMORY_WRITE_ATTEMPT:
            if not _try_join_attribute(
                event, event_index, event_type, reverse_index, turn_data, warnings
            ):
                _collect_envelope_violations(
                    event,
                    event_index,
                    event_type,
                    pending_violation_buffer,
                    warnings,
                )
            continue

        # ------------------------------------------------------------------
        # CONTEXT_SNAPSHOT — turn-establishing event. Two attribution
        # paths possible (per v3.6 same-event attribution rule):
        #   1. CONTEXT_SNAPSHOT has llm_turn_id → establishes the active
        #      turn. Any same-event policy_violations attribute to that
        #      same turn (intra-event). Buffered violations from prior
        #      events drain into this turn (inter-event).
        #   2. CONTEXT_SNAPSHOT lacks llm_turn_id → transparent to the
        #      bracketing model. Any policy_violations on it are
        #      buffered (treated like any other non-turn-establishing
        #      event) — there's no current turn to attribute to.
        # ------------------------------------------------------------------
        if event_type == EVENT_TYPE_CONTEXT_SNAPSHOT:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                # Malformed CONTEXT_SNAPSHOT payload — skip token + turn
                # extraction. If the event also carried policy_violations
                # on the envelope, those still buffer per the inter-event
                # path below.
                _collect_envelope_violations(
                    event,
                    event_index,
                    event_type,
                    pending_violation_buffer,
                    warnings,
                )
                continue

            turn_id = payload.get("llm_turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                # No turn_id → CONTEXT_SNAPSHOT does not establish a
                # turn. If it carries populated tokens without a turn
                # id, flag as untokened_pair (integrator misconfig).
                if _has_populated_tokens(payload):
                    untokened_pair_count += 1
                    warnings.append(
                        {
                            "code": "untokened_pair",
                            "event_index": event_index,
                            "detail": "CONTEXT_SNAPSHOT carried populated "
                            "tokens without llm_turn_id",
                        }
                    )
                # Violations on this event (if any): join to a turn by
                # tool_use_id if possible (live Claude Code POL-003/POL-005 on
                # a pre/post snapshot), else buffer per the inter-event path.
                if not _try_join_attribute(
                    event, event_index, event_type, reverse_index, turn_data, warnings
                ):
                    _collect_envelope_violations(
                        event,
                        event_index,
                        event_type,
                        pending_violation_buffer,
                        warnings,
                    )
                continue

            # Turn_id present → establish or re-encounter this turn.
            if turn_id not in turn_data:
                turn_data[turn_id] = _new_turn_data()
                turn_data[turn_id]["first_seen_index"] = event_index

            td = turn_data[turn_id]

            # Drain pending buffer into this turn (inter-event attribution).
            if pending_violation_buffer:
                for entry in pending_violation_buffer:
                    td["rules"].add(entry["rule"])
                    td["rule_metadata"].append(
                        {
                            "rule": entry["rule"],
                            "source_event_type": entry["source_event_type"],
                            "source_event_index": entry["source_event_index"],
                            "metadata": entry["metadata"],
                            "attribution": "buffered",
                        }
                    )
                pending_violation_buffer = []

            # Same-event attribution: violations carried on this same
            # CONTEXT_SNAPSHOT attribute to the same turn it establishes.
            envelope_violations = _safe_violation_list(event.get("policy_violations"))
            for rule in envelope_violations:
                if rule not in _KNOWN_POL_RULES:
                    warnings.append(
                        {
                            "code": "unknown_rule",
                            "event_index": event_index,
                            "detail": f"unknown policy rule string {rule!r}",
                        }
                    )
                    continue
                td["rules"].add(rule)
                td["rule_metadata"].append(
                    {
                        "rule": rule,
                        "source_event_type": event_type,
                        "source_event_index": event_index,
                        "metadata": _extract_event_metadata(event_type, payload),
                        "attribution": "same_event",
                    }
                )

            # Token dedupe precedence (first populated wins).
            if _has_populated_tokens(payload):
                if not td["tokens_populated"]:
                    for field in _TOKEN_FIELDS:
                        v = payload.get(field)
                        if isinstance(v, int) and v >= 0:
                            td["tokens"][field] = v
                    td["tokens_populated"] = True
                else:
                    # Already have tokens for this turn; check for conflict.
                    conflict = False
                    for field in _TOKEN_FIELDS:
                        v = payload.get(field)
                        if (
                            isinstance(v, int)
                            and v >= 0
                            and td["tokens"][field] != v
                        ):
                            conflict = True
                            break
                    if conflict:
                        dedupe_conflict_count += 1
                        warnings.append(
                            {
                                "code": "dedupe_conflict",
                                "event_index": event_index,
                                "detail": f"conflicting tokens for turn "
                                f"{turn_id[:12]}",
                            }
                        )
            continue

        # All other event types (INTENT_DECLARED, GOVERNANCE_ERROR) —
        # do not carry policy_violations per the v3.6 verify-step
        # finding. Skip silently for the burn-rate walker.
        continue

    # -----------------------------------------------------------------
    # Post-walk: any remaining buffered violations are unpaired.
    # -----------------------------------------------------------------
    # FIX-4 (v0.2.8): the unpaired violations are MEASURED data — count
    # them per rule so a turn-less session still reports which rules
    # fired (token attribution is what's deferred, not the counts).
    unpaired_by_rule: Dict[str, int] = {}
    if pending_violation_buffer:
        unpaired_violation_count += len(pending_violation_buffer)
        for entry in pending_violation_buffer:
            rule = entry["rule"]
            unpaired_by_rule[rule] = unpaired_by_rule.get(rule, 0) + 1
            warnings.append(
                {
                    "code": "unpaired_violation",
                    "event_index": entry["source_event_index"],
                    "detail": (
                        f"violation {entry['rule']} on event "
                        f"{entry['source_event_type']} had no following "
                        f"turn-establishing CONTEXT_SNAPSHOT"
                    ),
                }
            )
    # Canonical rule order for byte-stable output.
    unpaired_by_rule = {r: unpaired_by_rule[r] for r in sorted(unpaired_by_rule)}

    # Derive unknown_rule_count from warnings — single source of truth.
    # Unknown rules can surface from either `_collect_envelope_violations`
    # (inter-event path) or the CONTEXT_SNAPSHOT same-event branch; both
    # append a warning with code "unknown_rule". Counting from warnings
    # avoids two parallel counters that could drift.
    unknown_rule_count = sum(
        1 for w in warnings if w.get("code") == "unknown_rule"
    )

    # -----------------------------------------------------------------
    # Status determination + aggregation.
    # -----------------------------------------------------------------

    if not turn_data:
        # No CONTEXT_SNAPSHOTs with llm_turn_id at all.
        return _build_result(
            session_id=session_id,
            status=STATUS_NO_TURNS,
            turn_data={},
            unpaired_violation_count=unpaired_violation_count,
            unpaired_by_rule=unpaired_by_rule,
            untokened_pair_count=untokened_pair_count,
            dedupe_conflict_count=dedupe_conflict_count,
            malformed_event_count=malformed_event_count,
            unknown_rule_count=unknown_rule_count,
            warnings=warnings,
            total_event_count=total_event_count,
            subagent_activity_present=subagent_activity_present,
            profile_fingerprint=profile_fingerprint,
            profile_loaded=profile_loaded,
            profile_schema_version=profile_schema_version,
        )

    any_populated = any(td["tokens_populated"] for td in turn_data.values())
    any_violations = any(td["rules"] for td in turn_data.values())

    if not any_populated:
        return _build_result(
            session_id=session_id,
            status=STATUS_NO_TOKEN_DATA,
            turn_data=turn_data,
            unpaired_violation_count=unpaired_violation_count,
            unpaired_by_rule=unpaired_by_rule,
            untokened_pair_count=untokened_pair_count,
            dedupe_conflict_count=dedupe_conflict_count,
            malformed_event_count=malformed_event_count,
            unknown_rule_count=unknown_rule_count,
            warnings=warnings,
            total_event_count=total_event_count,
            subagent_activity_present=subagent_activity_present,
            profile_fingerprint=profile_fingerprint,
            profile_loaded=profile_loaded,
            profile_schema_version=profile_schema_version,
        )

    if not any_violations:
        status = STATUS_NO_VIOLATIONS
    else:
        status = STATUS_OK

    # Status partial: same partial-flip logic as v0.2.4 — any non-trivial
    # warning counter or malformed ratio > 25% promotes status to partial.
    if status == STATUS_OK:
        if total_event_count > 0:
            ratio = malformed_event_count / total_event_count
            if ratio > _PARTIAL_MALFORMED_THRESHOLD:
                status = STATUS_PARTIAL
        if status == STATUS_OK and (
            unpaired_violation_count > 0
            or untokened_pair_count > 0
            or dedupe_conflict_count > 0
            or malformed_event_count > 0
            or unknown_rule_count > 0
        ):
            status = STATUS_PARTIAL

    return _build_result(
        session_id=session_id,
        status=status,
        turn_data=turn_data,
        unpaired_violation_count=unpaired_violation_count,
            unpaired_by_rule=unpaired_by_rule,
        untokened_pair_count=untokened_pair_count,
        dedupe_conflict_count=dedupe_conflict_count,
        malformed_event_count=malformed_event_count,
        unknown_rule_count=unknown_rule_count,
        warnings=warnings,
        total_event_count=total_event_count,
        subagent_activity_present=subagent_activity_present,
        profile_fingerprint=profile_fingerprint,
        profile_loaded=profile_loaded,
        profile_schema_version=profile_schema_version,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _safe_violation_list(raw: Any) -> List[str]:
    """Return ``raw`` as a list of strings, defending against schema drift.

    The envelope's ``policy_violations`` should always be a List[str]
    per the schema, but the analyzer treats anything else as an empty
    list rather than raising. Non-string entries within an otherwise
    well-formed list are dropped silently.
    """
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, str) and r]


def _build_join_index(
    events_list: List[Dict[str, Any]],
) -> Tuple[Dict[str, str], Dict[str, Optional[str]]]:
    """Pre-pass — build the v0.2.6.1 `tool_use_id → llm_turn_id` join index.

    Scans token-bearing CONTEXT_SNAPSHOTs (those carrying ``llm_turn_id`` AND a
    ``tool_use_ids`` list — the Claude Code SessionEnd batch) and records, for
    each tool_use_id that turn issued, which turn (requestId) it belongs to.
    Also captures each turn's ``provider`` (for CP4b convention-aware ranking).

    This lets the walk attribute a live violation to its model turn **by id,
    not by event position** (D3) — essential for Claude Code, where the live
    violation events precede the token-bearing snapshots in the trace. Returns
    empty maps for traces that carry no such snapshots (MCP/LangChain), so the
    positional path is used unchanged.
    """
    reverse_index: Dict[str, str] = {}
    turn_provider: Dict[str, Optional[str]] = {}
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
        if turn_id not in turn_provider:
            prov = payload.get("provider")
            turn_provider[turn_id] = prov if isinstance(prov, str) else None
        tuids = payload.get("tool_use_ids")
        if isinstance(tuids, list):
            for tid in tuids:
                if isinstance(tid, str) and tid and tid not in reverse_index:
                    reverse_index[tid] = turn_id
    return reverse_index, turn_provider


def _detect_subagent_activity(events_list: List[Dict[str, Any]]) -> bool:
    """True iff the trace shows subagent (Task/Agent) tool activity.

    v0.2.6.1 D5: subagent transcripts are out of scope — their token burn is
    NOT captured. When subagents ran, the report must say so explicitly so a
    partial burn is never mistaken for full-session burn. Signal: a
    SCOPE_ASSERTED whose tool targets the agent runtime.
    """
    for event in events_list:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") != EVENT_TYPE_SCOPE_ASSERTED:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("target_system") == "agent_runtime":
            return True
        if payload.get("tool_id") in ("Task", "Agent"):
            return True
    return False


def _try_join_attribute(
    event: Dict[str, Any],
    event_index: int,
    event_type: str,
    reverse_index: Dict[str, str],
    turn_data: Dict[str, Dict[str, Any]],
    warnings: List[Dict[str, Any]],
) -> bool:
    """Attribute an event's violations to its turn by `tool_use_id` join.

    Returns True (and attributes directly to the joined turn) iff the event
    carries envelope violations AND a ``tool_use_id`` that resolves to a turn
    via ``reverse_index``. Returns False otherwise, so the caller falls back to
    the positional buffer — preserving byte-identical behavior for traces with
    no join keys (MCP/LangChain). Never guesses a join (D3): an absent/unmatched
    id simply yields False.
    """
    violations = _safe_violation_list(event.get("policy_violations"))
    if not violations:
        return False
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return False
    turn_id = reverse_index.get(tool_use_id)
    if turn_id is None:
        return False
    # Ensure the turn exists; its tokens fill when its snapshot is walked.
    if turn_id not in turn_data:
        turn_data[turn_id] = _new_turn_data()
        turn_data[turn_id]["first_seen_index"] = event_index
    td = turn_data[turn_id]
    metadata = _extract_event_metadata(event_type, payload)
    for rule in violations:
        if rule not in _KNOWN_POL_RULES:
            warnings.append(
                {
                    "code": "unknown_rule",
                    "event_index": event_index,
                    "detail": f"unknown policy rule string {rule!r}",
                }
            )
            continue
        td["rules"].add(rule)
        td["rule_metadata"].append(
            {
                "rule": rule,
                "source_event_type": event_type,
                "source_event_index": event_index,
                "metadata": dict(metadata),
                "attribution": "joined",
            }
        )
    return True


def _collect_envelope_violations(
    event: Dict[str, Any],
    event_index: int,
    event_type: str,
    buffer: List[Dict[str, Any]],
    warnings: List[Dict[str, Any]],
) -> None:
    """Append any envelope-level violations on this event to the buffer.

    Buffered entries carry the rule string plus source-event metadata
    so that downstream attribution into a turn can preserve which
    event-type and per-event context originally surfaced the rule.

    Unknown rule strings are appended a warning but NOT added to the
    buffer (we only aggregate rules we have descriptions for; unknown
    strings are surfaced as warnings for operator visibility).
    """
    envelope_violations = _safe_violation_list(event.get("policy_violations"))
    if not envelope_violations:
        return
    payload = event.get("payload")
    metadata = _extract_event_metadata(event_type, payload if isinstance(payload, dict) else None)
    for rule in envelope_violations:
        if rule not in _KNOWN_POL_RULES:
            warnings.append(
                {
                    "code": "unknown_rule",
                    "event_index": event_index,
                    "detail": f"unknown policy rule string {rule!r}",
                }
            )
            continue
        buffer.append(
            {
                "rule": rule,
                "source_event_type": event_type,
                "source_event_index": event_index,
                "metadata": dict(metadata),  # defensive copy per entry
            }
        )


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
    turn_data: Dict[str, Dict[str, Any]],
    unpaired_violation_count: int,
    unpaired_by_rule: Dict[str, int],
    untokened_pair_count: int,
    dedupe_conflict_count: int,
    malformed_event_count: int,
    unknown_rule_count: int,
    warnings: List[Dict[str, Any]],
    total_event_count: int,
    subagent_activity_present: bool = False,
    profile_fingerprint: Optional[str] = None,
    profile_loaded: Optional[bool] = None,
    profile_schema_version: Optional[int] = None,
) -> Dict[str, Any]:
    """Assemble the final structured result dict.

    Order of keys is fixed for byte-stable output (Python 3.7+ dicts
    preserve insertion order, so building keys in a fixed order
    guarantees identical repr() across runs).
    """
    # Aggregate per-turn totals.
    total_tokens = 0
    violation_associated_tokens = 0
    violation_firing_turns = 0

    # Per-rule aggregation via inverted index.
    # rule -> {turn_count, token_cost, sample_turn_ids}
    per_rule_accum: Dict[str, Dict[str, Any]] = {}

    # Sort turns by first_seen_index for stable per-rule sample ordering.
    sorted_turns: List[Tuple[str, Dict[str, Any]]] = sorted(
        turn_data.items(),
        key=lambda kv: (kv[1]["first_seen_index"], kv[0]),
    )

    for turn_id, td in sorted_turns:
        turn_total = sum(td["tokens"][field] for field in _TOKEN_FIELDS)
        total_tokens += turn_total
        rules: Set[str] = td["rules"]
        if rules:
            violation_associated_tokens += turn_total
            violation_firing_turns += 1
            for rule in rules:
                slot = per_rule_accum.setdefault(
                    rule,
                    {
                        "turn_count": 0,
                        "token_cost": 0,
                        "sample_turn_ids": [],
                    },
                )
                slot["turn_count"] += 1
                slot["token_cost"] += turn_total
                if len(slot["sample_turn_ids"]) < _SAMPLE_TURN_LIMIT:
                    slot["sample_turn_ids"].append(turn_id)

    # Build the by_rule dict with rule strings in canonical order
    # (POL-001 → POL-005) for byte-stable output.
    by_rule: Dict[str, Dict[str, Any]] = {}
    for rule in sorted(per_rule_accum.keys()):
        accum = per_rule_accum[rule]
        by_rule[rule] = {
            "description": _POL_DESCRIPTIONS.get(rule, ""),
            "turn_count": accum["turn_count"],
            "token_cost": accum["token_cost"],
            "sample_turn_ids": list(accum["sample_turn_ids"]),
        }

    # Notes: non-additivity callout fires when more than one rule has
    # non-zero token_cost. Operators see the caveat at the moment they
    # look at the per-rule rows. Two versions to match render-target
    # verbosity tolerance (per plan v3.3 micro-patch).
    notes: List[str] = []
    notes_short: List[str] = []
    rules_with_tokens = [
        r for r, slot in by_rule.items() if slot["token_cost"] > 0
    ]
    if len(rules_with_tokens) > 1:
        notes.append(
            "By-rule token totals are not additive when multiple rules fire "
            "on the same turn. Sum the by-rule rows and you may exceed "
            "violation_associated_tokens or even total_tokens — that is "
            "expected, not a bug. Top-level violation_associated_tokens "
            "counts unique violation-firing turns only."
        )
        notes_short.append(
            "Note: by-rule token totals are not additive. A turn with "
            "multiple rule firings is counted once in violation-associated "
            "tokens, but appears under each fired rule."
        )

    return {
        "schema_version": _SCHEMA_VERSION,
        "analyzer": _ANALYZER_NAME,
        "analyzer_version": _ANALYZER_VERSION,
        "session_id": session_id or "",
        "status": status,
        "total_tokens": total_tokens,
        "violation_associated_tokens": violation_associated_tokens,
        "violation_firing_turns": violation_firing_turns,
        "by_rule": by_rule,
        "notes": notes,
        "notes_short": notes_short,
        "warnings": warnings,
        "unpaired_violation_count": unpaired_violation_count,
        "unpaired_by_rule": dict(unpaired_by_rule),
        "untokened_pair_count": untokened_pair_count,
        "dedupe_conflict_count": dedupe_conflict_count,
        "malformed_event_count": malformed_event_count,
        "unknown_rule_count": unknown_rule_count,
        "subagent_activity_present": subagent_activity_present,
        "profile_fingerprint": profile_fingerprint,
        "profile_loaded": profile_loaded,
        "profile_schema_version": profile_schema_version,
    }
