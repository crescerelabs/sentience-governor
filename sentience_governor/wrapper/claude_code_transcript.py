"""Claude Code transcript parser + turn-join model (v0.2.6.1 CP1).

Claude Code writes a JSONL transcript (one JSON object per line). Each
**assistant** message line carries `message.usage` (Anthropic
`Message.usage` shape), a `requestId` (the per-turn / per-model-invocation
unit), and `message.content` blocks — some of which are `tool_use` blocks
carrying a `toolu_…` `id`. That `id` is the SAME value the live
Pre/PostToolUse hook sees as `tool_use_id`.

This module turns a transcript into per-`requestId` turn records and exposes
the join that lets an analyzer answer "which turn issued this tool call, and
how much did that turn burn?" — by `tool_use_id`, never by event position.

**Scope (CP1):** PURE parsing + the join model, proven against fixtures. It
does NOT read hooks, does NOT emit Sentience events, and does NOT touch the
production analyzers — that is CP2/CP4. The only I/O is the thin
`parse_transcript_file` convenience that streams a file into the pure core.

Design rules (mirroring the analyzer modules so golden/replay tests hold):

* **Streaming.** The core consumes an *iterable of lines* once and never
  materializes the whole transcript. Real sessions reach thousands of
  `requestId`s (verified: 6,866 in one session) — do not assume a small file.
* **Fail-open / never raise.** Malformed JSON, missing fields, a truncated
  final line: logged into result counters/warnings and skipped. Parsing a
  busted transcript must never crash session end (D4).
* **Dedupe by `requestId`, never sum** (D1/F3). Usage repeats across a
  request's message lines; the first populated usage wins. A *conflicting*
  later usage is counted once and flagged (`dedupe_conflict`), never summed.
* **Pure + deterministic.** No logging side effects, no env reads, no clock.
  Output is byte-stable for identical input.

"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from sentience_governor.wrapper.token_extraction import (
    CANONICAL_TOKEN_FIELDS,
    PROVIDER_ANTHROPIC,
    derive_turn_token_burn,
    extract_anthropic_usage,
)

# The Claude Code runtime is Anthropic; the SessionEnd parser stamps this so
# the convention is fully determinable downstream (D2 / §11 resolution).
TRANSCRIPT_PROVIDER = PROVIDER_ANTHROPIC

# Transcript line `type` we attribute tokens from. Everything else
# (user / system / summary / compaction) is skipped (D6).
_ASSISTANT_TYPE = "assistant"

# Warning codes (stable strings — asserted in tests).
WARN_MALFORMED_LINE = "malformed_line"
WARN_INCOMPLETE_FINAL_LINE = "incomplete_final_line"
WARN_MISSING_REQUEST_ID = "missing_request_id"
WARN_DEDUPE_CONFLICT = "dedupe_conflict"
WARN_MISSING_USAGE = "missing_usage"


def _new_turn(request_id: str, line_index: int) -> Dict[str, Any]:
    """A fresh per-`requestId` turn record (plain dict for byte-stability)."""
    return {
        "request_id": request_id,
        "provider": TRANSCRIPT_PROVIDER,
        "model_identifier": None,
        "tokens": {field: None for field in CANONICAL_TOKEN_FIELDS},
        "tokens_populated": False,
        "tool_use_ids": [],  # ordered, unique
        "tool_names": [],  # parallel to tool_use_ids
        "message_count": 0,
        "first_seen_index": line_index,
    }


def _usage_is_populated(usage: Dict[str, Optional[int]]) -> bool:
    """True iff at least one canonical field is a non-negative int."""
    for field in CANONICAL_TOKEN_FIELDS:
        v = usage.get(field)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return True
    return False


def _usage_conflicts(
    existing: Dict[str, Optional[int]], incoming: Dict[str, Optional[int]]
) -> bool:
    """True iff a populated incoming field disagrees with an existing one.

    Only compares fields populated on BOTH sides; a field present on one and
    absent on the other is a fill, not a conflict.
    """
    for field in CANONICAL_TOKEN_FIELDS:
        a = existing.get(field)
        b = incoming.get(field)
        if (
            isinstance(a, int)
            and not isinstance(a, bool)
            and isinstance(b, int)
            and not isinstance(b, bool)
            and a != b
        ):
            return True
    return False


def _extract_tool_uses(content: Any) -> List[Dict[str, str]]:
    """Pull `{id, name}` for each `tool_use` block in a message's content.

    Defensive against non-list content and malformed blocks (R2 format
    drift) — returns only well-formed tool_use blocks, never raises.
    """
    out: List[Dict[str, str]] = []
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        tid = block.get("id")
        if not isinstance(tid, str) or not tid:
            continue
        name = block.get("name")
        out.append({"id": tid, "name": name if isinstance(name, str) else ""})
    return out


def parse_transcript(lines: Iterable[str]) -> Dict[str, Any]:
    """Parse a Claude Code transcript into per-`requestId` turn records.

    Args:
        lines: an iterable of raw JSONL strings (one transcript line each).
            Consumed exactly once — pass a generator to stream a large file
            without materializing it.

    Returns a byte-stable result dict:

      * ``provider`` — always ``"anthropic"`` for this parser.
      * ``turns`` — ``{request_id: turn_record}``, insertion-ordered by the
        line where each request first appeared.
      * ``turn_order`` — request_ids in first-seen order.
      * ``tool_use_index`` — ``{tool_use_id: request_id}`` (the join's reverse
        map; see :func:`join_tool_use_ids`).
      * counters: ``total_line_count``, ``assistant_line_count``,
        ``skipped_line_count``, ``malformed_line_count``,
        ``missing_request_id_count``, ``dedupe_conflict_count``,
        ``incomplete_final_line`` (bool).
      * ``warnings`` — ``[{code, line_index, detail}]``.

    Never raises. Lines that don't parse, aren't assistant messages, or lack a
    `requestId` are counted/warned and skipped.
    """
    turns: Dict[str, Dict[str, Any]] = {}
    turn_order: List[str] = []
    tool_use_index: Dict[str, str] = {}
    warnings: List[Dict[str, Any]] = []

    total_line_count = 0
    assistant_line_count = 0
    skipped_line_count = 0
    malformed_line_count = 0
    missing_request_id_count = 0
    dedupe_conflict_count = 0

    # Track the last malformed line so a trailing parse failure can be
    # reclassified as an incomplete (still-being-written) tail, which D4
    # treats as recoverable rather than corrupt.
    last_index = -1
    last_malformed_index = -1
    incomplete_final_line = False

    for line_index, raw in enumerate(lines):
        last_index = line_index
        if raw is None:
            continue
        stripped = raw.strip()
        if not stripped:
            continue
        total_line_count += 1

        try:
            obj = json.loads(stripped)
        except (ValueError, TypeError):
            malformed_line_count += 1
            last_malformed_index = line_index
            warnings.append(
                {
                    "code": WARN_MALFORMED_LINE,
                    "line_index": line_index,
                    "detail": "line is not valid JSON",
                }
            )
            continue

        if not isinstance(obj, dict):
            skipped_line_count += 1
            continue

        # D6 — only assistant messages carry attributable usage.
        if obj.get("type") != _ASSISTANT_TYPE:
            skipped_line_count += 1
            continue

        message = obj.get("message")
        if not isinstance(message, dict):
            skipped_line_count += 1
            continue

        assistant_line_count += 1

        request_id = obj.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            missing_request_id_count += 1
            warnings.append(
                {
                    "code": WARN_MISSING_REQUEST_ID,
                    "line_index": line_index,
                    "detail": "assistant message without a requestId",
                }
            )
            continue

        if request_id not in turns:
            turns[request_id] = _new_turn(request_id, line_index)
            turn_order.append(request_id)
        turn = turns[request_id]
        turn["message_count"] += 1

        # Model identity (first non-empty wins).
        if turn["model_identifier"] is None:
            model = message.get("model")
            if isinstance(model, str) and model:
                turn["model_identifier"] = model

        # Tool-use blocks → accumulate unique ids + the reverse join index.
        for tu in _extract_tool_uses(message.get("content")):
            tid = tu["id"]
            if tid not in tool_use_index:
                tool_use_index[tid] = request_id
                turn["tool_use_ids"].append(tid)
                turn["tool_names"].append(tu["name"])

        # Usage — dedupe by requestId (first populated wins; conflict flagged).
        raw_usage = message.get("usage")
        if raw_usage is None:
            continue
        usage = extract_anthropic_usage(raw_usage)
        if not _usage_is_populated(usage):
            continue
        if not turn["tokens_populated"]:
            turn["tokens"] = dict(usage)
            turn["tokens_populated"] = True
        elif _usage_conflicts(turn["tokens"], usage):
            dedupe_conflict_count += 1
            warnings.append(
                {
                    "code": WARN_DEDUPE_CONFLICT,
                    "line_index": line_index,
                    "detail": (
                        f"conflicting usage for requestId {request_id[:12]} — "
                        "keeping first, not summing"
                    ),
                }
            )
        # else: identical (or pure fill) repeat → counted once, ignore.

    # A trailing malformed line is most likely a transcript still being
    # flushed at SessionEnd — reclassify it (D4 bounded-tail semantics).
    if malformed_line_count > 0 and last_malformed_index == last_index:
        incomplete_final_line = True
        for w in warnings:
            if (
                w["code"] == WARN_MALFORMED_LINE
                and w["line_index"] == last_malformed_index
            ):
                w["code"] = WARN_INCOMPLETE_FINAL_LINE
                w["detail"] = "final line did not parse (transcript may be mid-flush)"
                break

    # Flag tool-bearing turns that never got usage (joinable, but burn unknown).
    for request_id in turn_order:
        turn = turns[request_id]
        if turn["tool_use_ids"] and not turn["tokens_populated"]:
            warnings.append(
                {
                    "code": WARN_MISSING_USAGE,
                    "line_index": turn["first_seen_index"],
                    "detail": (
                        f"requestId {request_id[:12]} issued tool calls but "
                        "carried no usage — attribution joinable, burn unknown"
                    ),
                }
            )

    return {
        "provider": TRANSCRIPT_PROVIDER,
        "turns": turns,
        "turn_order": turn_order,
        "tool_use_index": tool_use_index,
        "total_line_count": total_line_count,
        "assistant_line_count": assistant_line_count,
        "skipped_line_count": skipped_line_count,
        "malformed_line_count": malformed_line_count,
        "missing_request_id_count": missing_request_id_count,
        "dedupe_conflict_count": dedupe_conflict_count,
        "incomplete_final_line": incomplete_final_line,
        "warnings": warnings,
    }


def parse_transcript_file(path: str) -> Dict[str, Any]:
    """Stream a transcript file through :func:`parse_transcript`.

    The only I/O boundary in this module. Reads line-by-line (never slurps
    the whole file) so a multi-thousand-`requestId` transcript stays bounded.
    A missing/unreadable file is fail-open: returns an empty parse result with
    a ``malformed_line``-style warning rather than raising (D4).
    """
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError as exc:
        result = parse_transcript([])
        result["warnings"].append(
            {
                "code": WARN_MALFORMED_LINE,
                "line_index": -1,
                "detail": f"could not open transcript: {exc.__class__.__name__}",
            }
        )
        return result
    with handle:
        return parse_transcript(handle)


def join_tool_use_ids(
    parse_result: Dict[str, Any], tool_use_ids: Iterable[str]
) -> Dict[str, Optional[str]]:
    """Map live-captured ``tool_use_id``s to their ``requestId`` turns.

    This is the analyzer-join model (D3), proven here against fixtures ahead
    of the live hook (CP2). Each tool_use_id resolves to its turn via the
    reverse index built during parsing — NOT by event position.

    Returns ``{tool_use_id: request_id_or_None}``. An unmatched id maps to
    ``None`` — the caller marks that attribution **partial/unavailable**, and
    must never fall back to guessing a turn (D3: "never guess a join").
    """
    index = parse_result.get("tool_use_index", {})
    return {tid: index.get(tid) for tid in tool_use_ids}


def turn_token_burn(parse_result: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    """Convention-aware burn for one turn (thin wrapper over the helper).

    Returns the :func:`derive_turn_token_burn` dict, or a zero/partial result
    if the ``request_id`` is unknown. The turn is counted **once** regardless
    of how many tool calls it issued (D-multi): burn is a property of the
    turn, never multiplied across its tool calls.
    """
    turn = parse_result.get("turns", {}).get(request_id)
    if turn is None:
        return derive_turn_token_burn(
            {field: None for field in CANONICAL_TOKEN_FIELDS}, None
        )
    return derive_turn_token_burn(turn["tokens"], turn.get("provider"))


__all__ = [
    "TRANSCRIPT_PROVIDER",
    "WARN_MALFORMED_LINE",
    "WARN_INCOMPLETE_FINAL_LINE",
    "WARN_MISSING_REQUEST_ID",
    "WARN_DEDUPE_CONFLICT",
    "WARN_MISSING_USAGE",
    "parse_transcript",
    "parse_transcript_file",
    "join_tool_use_ids",
    "turn_token_burn",
]
