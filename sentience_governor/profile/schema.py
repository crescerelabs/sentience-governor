"""Governance profile schema — constants, enum values, defaults.

This module defines the v0.2.5 governance profile schema (schema
version 1) as a set of constants and default-value helpers. The
schema is intentionally narrow: it covers governance posture only,
not application configuration. Existing surfaces (env vars,
wrap_mcp_client kwargs, sync config) are NOT migrated; that's a
deferred candidate.

Schema sections in v0.2.5:

* ``session_intent`` — when the agent must declare intent
* ``task_boundary`` — signals indicating a new task
* ``high_consequence`` — tool patterns that always surface
  prominently

Reserved schema slots (recognized, ignored in v0.2.5):

* ``extends`` — profile inheritance
* ``policies`` — policy rule customization
* ``custom_rules`` — user-defined rules

Reserved ``on_match`` values (recognized but warned-on-use):

* ``prompt``, ``block``, ``deny`` — reserved for future runtime
  enforcement; treated as ``flag`` by this runtime
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

# v0.2.5 ships schema_version 1. Additive changes (new sections,
# new fields, new enum values) stay at version 1. Breaking changes
# (renamed sections, removed fields, changed semantics) would bump
# to 2 — not anticipated in the v0.2.x line.
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# session_intent.demand_at values
# ---------------------------------------------------------------------------

# When the profile says the agent must declare intent before
# operations begin. These three values are the v0.2.5 vocabulary;
# additional values are reserved for future candidates.
DEMAND_AT_FIRST_WRITE = "first_write"      # POL-001 fires only on first WRITE/DELETE/EXECUTE without prior intent
DEMAND_AT_SESSION_START = "session_start"  # POL-001 fires on every event until intent declared (v0.2.4 default)
DEMAND_AT_NEVER = "never"                  # POL-001 never fires; profile explicitly opts out

VALID_DEMAND_AT_VALUES = frozenset([
    DEMAND_AT_FIRST_WRITE,
    DEMAND_AT_SESSION_START,
    DEMAND_AT_NEVER,
])


# ---------------------------------------------------------------------------
# task_boundary.signals values
# ---------------------------------------------------------------------------

# Signals that indicate a meaningful task boundary. The runtime
# (CP2) watches for these signals and, when one fires, emits the
# TASK_BOUNDARY_CROSSED advisory flag on the next SCOPE_ASSERTED
# event.
SIGNAL_DIR_CHANGE = "dir_change"
SIGNAL_FILE_TYPE_SHIFT = "file_type_shift"
SIGNAL_TIME_GAP = "time_gap"
SIGNAL_READ_TO_WRITE_TRANSITION = "read_to_write_transition"

VALID_SIGNAL_VALUES = frozenset([
    SIGNAL_DIR_CHANGE,
    SIGNAL_FILE_TYPE_SHIFT,
    SIGNAL_TIME_GAP,
    SIGNAL_READ_TO_WRITE_TRANSITION,
])


# ---------------------------------------------------------------------------
# on_match values
# ---------------------------------------------------------------------------

# v0.2.5 ships exactly one valid on_match value: "flag". The
# reserved values below (prompt, block, deny) are reserved
# enforcement vocabulary; this runtime recognizes them
# but warns + falls back to "flag".
ON_MATCH_FLAG = "flag"

# Reserved values — accepted as known strings (no parser error)
# but this runtime treats them as "flag" and emits a
# warning.
ON_MATCH_PROMPT_RESERVED = "prompt"
ON_MATCH_BLOCK_RESERVED = "block"
ON_MATCH_DENY_RESERVED = "deny"

VALID_ON_MATCH_VALUES = frozenset([ON_MATCH_FLAG])

# Values the loader recognizes as reserved (for clean warnings
# rather than "unknown value" errors).
RESERVED_ON_MATCH_VALUES = frozenset([
    ON_MATCH_PROMPT_RESERVED,
    ON_MATCH_BLOCK_RESERVED,
    ON_MATCH_DENY_RESERVED,
])


# ---------------------------------------------------------------------------
# Reserved top-level keys
# ---------------------------------------------------------------------------

# These keys are recognized but ignored in v0.2.5. The loader
# warns when they appear. Each is a reserved slot for a planned
# capability that, once implemented, gives the key meaning.
RESERVED_TOP_LEVEL_KEYS = frozenset([
    "extends",         # profile inheritance
    "policies",        # policy rule customization
    "custom_rules",    # user-defined rules
])


# ---------------------------------------------------------------------------
# Top-level section names (active in v0.2.5)
# ---------------------------------------------------------------------------

SECTION_SESSION_INTENT = "session_intent"
SECTION_TASK_BOUNDARY = "task_boundary"
SECTION_HIGH_CONSEQUENCE = "high_consequence"

ACTIVE_SECTIONS = frozenset([
    SECTION_SESSION_INTENT,
    SECTION_TASK_BOUNDARY,
    SECTION_HIGH_CONSEQUENCE,
])

# All known top-level keys (active + reserved + schema_version).
# Used by lenient validation to distinguish "unknown key" (warn)
# from "known reserved key" (warn with different message).
KNOWN_TOP_LEVEL_KEYS = frozenset([
    "schema_version",
    *ACTIVE_SECTIONS,
    *RESERVED_TOP_LEVEL_KEYS,
])


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

# Defaults applied when a profile file is absent or a section is
# unspecified. These preserve v0.2.4 behavior for any session
# without a profile, so v0.2.5 is fully backward-compatible: no
# profile = no behavior change from v0.2.4.

DEFAULT_SESSION_INTENT = {
    "required": True,
    "demand_at": DEMAND_AT_SESSION_START,  # v0.2.4 default behavior
    "prompt_template": None,
}

DEFAULT_TASK_BOUNDARY = {
    "signals": [],                  # empty = no task-boundary detection
    "time_gap_seconds": 300,        # 5 minutes — only consulted if "time_gap" in signals
    "dir_change_depth": 2,          # only consulted if "dir_change" in signals
    "on_match": ON_MATCH_FLAG,      # value is moot when signals is empty
}

DEFAULT_HIGH_CONSEQUENCE = {
    "tools": [],                    # empty = no high-consequence patterns
    "on_match": ON_MATCH_FLAG,      # value is moot when tools is empty
}


def default_profile_data() -> dict:
    """Return the canonical defaults as a fresh nested dict.

    Returns a NEW dict on every call (no shared state). Used by the
    loader when no profile file is present, and by tests verifying
    the default shape.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        SECTION_SESSION_INTENT: dict(DEFAULT_SESSION_INTENT),
        SECTION_TASK_BOUNDARY: {
            **DEFAULT_TASK_BOUNDARY,
            "signals": list(DEFAULT_TASK_BOUNDARY["signals"]),
        },
        SECTION_HIGH_CONSEQUENCE: {
            **DEFAULT_HIGH_CONSEQUENCE,
            "tools": list(DEFAULT_HIGH_CONSEQUENCE["tools"]),
        },
    }
