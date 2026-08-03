"""IR-5 (v0.2.9): the machine-readable methodology surface behind
``sentience explain``.

Single source of truth for *how Sentience counts* — the token classes,
the dedupe rule, the per-turn (not per-tool) attribution boundary, the
operation-type enum, and the join-key semantics. CLI-first: the
``explain`` command renders this dict; the MCP adapter consumes the same
dict later.

This is the authoritative counter to "the token capture didn't land"
misdiagnoses: it states, deterministically, what is measured and where
attribution stops. It carries no session data — it is pure methodology.

Wording discipline (P1): attribution stops at the turn. "Tokens on turns
involving tool X" is measurable; "tokens tool X spent" is not, because
the model meters usage per turn, not per tool call.
"""

from __future__ import annotations

from typing import Any, Dict

# Bump when the methodology described here changes in a way a consumer
# must notice (new class, changed dedupe key, changed boundary).
METHODOLOGY_VERSION = 1


def build_methodology() -> Dict[str, Any]:
    """Return the methodology as a structured, JSON-serializable dict.

    Stable shape and key order for byte-stable ``--json`` output.
    """
    return {
        "methodology_version": METHODOLOGY_VERSION,
        "summary": (
            "How Sentience counts: what is measured, how per-turn usage is "
            "deduped, and where attribution stops."
        ),
        "token_classes": {
            "prompt": (
                "Input tokens billed at the full (non-cached) rate for a "
                "model turn."
            ),
            "completion": "Output tokens the model generated on a turn.",
            "cached_read": (
                "Input tokens served from the prompt cache (a cache read) "
                "on a turn."
            ),
            "cached_write": (
                "Input tokens written into the prompt cache on a turn."
            ),
        },
        "token_classes_note": (
            "The four classes sum to the turn's total compute. Reported "
            "totals sum these across the session's turns."
        ),
        "dedupe_rule": (
            "Per-turn token usage is deduped by llm_turn_id before summing. "
            "llm_turn_id is the model-invocation (requestId) boundary; "
            "repeated snapshots of the same turn are counted once."
        ),
        "attribution_boundary": (
            "Tokens are metered per model turn, not per tool call. A single "
            "turn can fire several tool calls under one usage figure, so "
            "Sentience attributes tokens to turns — never to an individual "
            "tool. 'Tokens on turns involving tool X' is measurable; "
            "'tokens tool X spent' is not."
        ),
        "operation_types": ["READ", "WRITE", "DELETE", "EXECUTE"],
        "operation_types_note": (
            "Every SCOPE_ASSERTED event carries one operation_type. Tool "
            "calls are counted by this class; a turn that mixes classes "
            "counts under each."
        ),
        "join_keys": (
            "A SCOPE_ASSERTED event joins to its model turn by tool_use_id → "
            "llm_turn_id (by id, not by event position). Tool calls without "
            "a token-bearing turn are still counted, but carry no token "
            "attribution."
        ),
    }
