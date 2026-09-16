"""Governance profile module — operator-defined governance posture.

The governance profile is a YAML artifact at ``~/.sentience/profile.yaml``
that the runtime reads at session start to know what the operator
expects of an agent. It encodes three layers of hook behavior
(session intent, task boundary, high-consequence operations) and
forward-compatible schema slots for future capabilities
(``extends``, ``policies``, ``custom_rules``).

The profile module is intentionally standalone — it does not depend
on the rest of the runtime. Runtime integration (session manager,
policy evaluator, advisory flags) lands in v0.2.5 Checkpoint 2.

Module guarantees:

* **Read-only validation.** ``GovernanceProfile.validate()`` is
  read-only and never mutates the operator-authored profile file.
  Only ``export()`` and ``write_init_profile()`` write to disk.
* **Lenient by default.** Unknown top-level keys produce a warning
  but do not crash the loader. Strict mode is opt-in via
  ``validate(strict=True)``.
* **Forward-compatible.** Reserved schema slots (``extends``,
  ``policies``, ``custom_rules``) are recognized but ignored in
  v0.2.5; future candidates land additively without schema_version
  bump.
* **Operator sovereignty (P9).** Profile contents stay local;
  nothing in this module reaches out over the network.

"""

from sentience_governor.profile.loader import (
    DEFAULT_PROFILE_PATH,
    DEFAULT_RESOLUTION_PATH,
    GovernanceProfile,
    ProfileValidationResult,
    canonical_profile_data,
)
from sentience_governor.profile.resolver import (
    ResolvedProfile,
    resolve_profile,
)
from sentience_governor.profile.schema import (
    DEMAND_AT_FIRST_WRITE,
    DEMAND_AT_NEVER,
    DEMAND_AT_SESSION_START,
    ON_MATCH_FLAG,
    OPERATION_ACTIONS,
    OPERATION_DOMAINS,
    OPTIONAL_ADDITIVE_FIELDS,
    SCHEMA_VERSION,
    SET_VALUED_RULE_PREDICATES,
    SIGNAL_DIR_CHANGE,
    SIGNAL_FILE_TYPE_SHIFT,
    SIGNAL_READ_TO_WRITE_TRANSITION,
    SIGNAL_TIME_GAP,
    VALID_DEMAND_AT_VALUES,
    VALID_ON_MATCH_VALUES,
    VALID_SIGNAL_VALUES,
)

__all__ = [
    "GovernanceProfile",
    "ProfileValidationResult",
    "ResolvedProfile",
    "resolve_profile",
    "canonical_profile_data",
    "DEFAULT_PROFILE_PATH",
    "DEFAULT_RESOLUTION_PATH",
    "OPERATION_ACTIONS",
    "OPERATION_DOMAINS",
    "OPTIONAL_ADDITIVE_FIELDS",
    "SET_VALUED_RULE_PREDICATES",
    "SCHEMA_VERSION",
    "DEMAND_AT_FIRST_WRITE",
    "DEMAND_AT_NEVER",
    "DEMAND_AT_SESSION_START",
    "ON_MATCH_FLAG",
    "SIGNAL_DIR_CHANGE",
    "SIGNAL_FILE_TYPE_SHIFT",
    "SIGNAL_READ_TO_WRITE_TRANSITION",
    "SIGNAL_TIME_GAP",
    "VALID_DEMAND_AT_VALUES",
    "VALID_ON_MATCH_VALUES",
    "VALID_SIGNAL_VALUES",
]
