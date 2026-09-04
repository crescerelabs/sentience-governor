"""Tool classification: explicit-first, honest when absent (CP4).

What a developer writes on a tool::

    Tool(
        crm_fetch,
        metadata={"sentience_governor": {
            "operation": "READ",
            "target_system": "crm",
            "classification": ["internal"],
        }},
    )

Three states, and keeping them apart is the whole point:

* **Present and well-formed** — used, recorded as explicit.
* **Genuinely absent** — the call is still recorded, honestly unclassified.
  `target_system` falls back to the tool's own name, which is a fact about
  the call rather than a guessed bucket.
* **Present but malformed** — D2 visible fail-open. Never collapsed into
  "absent", because *absent* and *present but unusable* are different
  situations and a developer who wrote metadata believes they classified
  something.

An **unknown key** is a fourth case and does not belong with the third
(Rev 6). It is reported visibly, and every recognised field that
independently validates survives: discarding a truthful classification
because of an unrelated stray key makes the evidence worse and protects
nothing, since the developer is told either way. This is deliberately
**not** how the run declaration behaves, and the asymmetry is the point:
a declaration sets the baseline the whole run is evaluated against, while
a classification governs one call.

**Nothing here derives an operation or a target system from a tool name.**
A tool called ``db_delete_record`` does not become ``(DELETE, db)``. A
rejected classification does not license guessing either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from sentience_governor.schema.events import ClassificationSource, OperationType

from pydantic_ai_governor.declaration import NAMESPACE


class Operation(str, Enum):
    """The integration's internal operation semantic.

    Identical to core's ``OperationType`` except for ``UNKNOWN``, which
    core cannot represent: ``OperationType`` has four members and
    ``operation_type`` is a required, non-nullable field on
    ``ScopeAssertedPayload``. There is no way to say "undeclared" in the
    event, so the distinction lives here and is mapped at the boundary by
    :func:`to_core_operation`.
    """

    READ = "READ"
    WRITE = "WRITE"
    DELETE = "DELETE"
    EXECUTE = "EXECUTE"
    UNKNOWN = "UNKNOWN"


def to_core_operation(operation: "Operation") -> Tuple[OperationType, List[str]]:
    """Map the internal semantic onto what core can actually serialize.

    Returns the ``operation_type`` and the ``asserted_permissions`` that
    belong with it.

    **`UNKNOWN` maps to `READ` with no permissions, and that `READ` is a
    compatibility fallback rather than a claim that the tool read
    anything.** It is chosen because `READ` is the only non-mutating
    member: `_eval_scope` treats WRITE, DELETE and EXECUTE as mutating, so
    any of those would manufacture `SCOPE_OPERATION_UNEXPECTED` and
    POL-001 in an undeclared session on the strength of a fallback rather
    than anything the developer did.

    The empty permissions list distinguishes this from an explicitly
    declared `READ`, which carries ``["read"]``. **That is a marker for a
    reader of the trace, not a mechanism**: nothing in this release
    interprets empty permissions as UNKNOWN, and nothing may.

    **This mapping exists only because core has no undeclared-operation
    semantic.** When core gains one, it is removed and `UNKNOWN`
    serializes directly.
    """
    if operation is Operation.UNKNOWN:
        return OperationType.READ, []
    return OperationType(operation.value), [operation.value.lower()]


def estimate_context_tokens(data: Any) -> int:
    """An estimated context token count, using core's own estimator.

    Deliberately identical to the shipped `wrapper/mcp.py:406-412`, so the
    field carries the meaning the product already gives it rather than a
    second convention. **It is an estimate, not measured model-token
    usage**: measured usage arrives separately on `llm_prompt_tokens` and
    `llm_completion_tokens`.
    """
    try:
        return max(1, len(json.dumps(data)) // 4)
    except Exception:
        return 1

_OPERATION = "operation"
_TARGET_SYSTEM = "target_system"
_CLASSIFICATION = "classification"

# As with the declaration block, this is the complete recognised set.
_RECOGNISED = frozenset({_OPERATION, _TARGET_SYSTEM, _CLASSIFICATION})

# Exact values only. `"read"` is not `"READ"`: accepting near-misses would
# be inference by another name, and the developer would never learn that
# what they wrote was not what was recorded.
_OPERATIONS = frozenset(member.value for member in OperationType)


@dataclass(frozen=True)
class Classification:
    """How one tool call is classified, if at all.

    ``source`` is ``explicit`` only when a well-formed block supplied it.
    Absent and rejected both yield ``unclassified``, which is the honest
    answer and the one core's POL-003 semantics expect.

    ``operation`` is ``UNKNOWN`` when nothing was declared. It is never
    inferred from the tool's name.
    """

    operation: Operation = Operation.UNKNOWN
    target_system: Optional[str] = None
    data_classifications: Tuple[str, ...] = ()
    source: ClassificationSource = ClassificationSource.unclassified

    @property
    def is_explicit(self) -> bool:
        return self.source == ClassificationSource.explicit

    @property
    def operation_declared(self) -> bool:
        return self.operation is not Operation.UNKNOWN

    def core_operation(self) -> Tuple[OperationType, List[str]]:
        """What to put on the event, and the permissions that belong."""
        return to_core_operation(self.operation)

    def target_for(self, tool_name: str) -> str:
        """The declared target system, else the tool's own name.

        The fallback is a fact about the call. It is deliberately not a
        bucket inferred from what the tool is called.
        """
        return self.target_system or tool_name


def validate(block: Any) -> Optional[str]:
    """Describe the first REJECTING problem with a classification block.

    Names the field and the contract it broke, never the value. Returns
    None when the recognised fields are usable.

    Unknown keys are **not** rejecting (Rev 6); see `unknown_keys`.
    """
    if not isinstance(block, Mapping):
        return (
            f"the '{NAMESPACE}' tool metadata must be a mapping of "
            f"'{_OPERATION}', '{_TARGET_SYSTEM}' and '{_CLASSIFICATION}'"
        )

    if _OPERATION in block:
        operation = block[_OPERATION]
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            allowed = ", ".join(f"'{value}'" for value in sorted(_OPERATIONS))
            return f"'{_OPERATION}' must be exactly one of {allowed}"

    if _TARGET_SYSTEM in block:
        target = block[_TARGET_SYSTEM]
        if not isinstance(target, str) or not target.strip():
            return f"'{_TARGET_SYSTEM}' must be a non-empty string"

    if _CLASSIFICATION in block:
        values = block[_CLASSIFICATION]
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            return f"'{_CLASSIFICATION}' must be a list of strings"
        for item in values:
            if not isinstance(item, str) or not item.strip():
                return f"'{_CLASSIFICATION}' must contain only non-empty strings"

    return None


def unknown_keys(block: Any) -> Tuple[str, ...]:
    """Recognised-set outliers in a block, in stable order. Names only."""
    if not isinstance(block, Mapping):
        return ()
    return tuple(sorted(str(key) for key in block if key not in _RECOGNISED))


def resolve(tool_metadata: Any) -> Tuple[Classification, Optional[str]]:
    """This call's classification, and any rejection reason.

    A malformed block is rejected **as a unit**: a good ``operation``
    beside a broken ``classification`` yields nothing, because a partial
    classification recorded as explicit would claim more than the
    developer actually supplied.
    """
    raw = None
    if isinstance(tool_metadata, Mapping):
        raw = tool_metadata.get(NAMESPACE)

    if raw is None:
        return Classification(), None

    problem = validate(raw)
    if problem is not None:
        # An invalid value in a recognised field still rejects the block as
        # a unit: we understood what was attempted and cannot trust it. The
        # tool name is still not mined for a substitute.
        return Classification(), (
            f"{problem}; the classification was rejected and this call is "
            "recorded as unclassified"
        )

    operation = raw.get(_OPERATION)
    values = raw.get(_CLASSIFICATION)
    classification = Classification(
        operation=Operation(operation) if operation else Operation.UNKNOWN,
        target_system=raw.get(_TARGET_SYSTEM),
        data_classifications=tuple(values) if values is not None else (),
        # Describes the provenance of the DATA CLASSIFICATIONS, not of the
        # block: `classification_source` travels with `data_classifications`
        # on the same payload, and the shipped MCP wrapper sets `explicit`
        # only when a hint supplies both (`wrapper/mcp.py:294-300`). So a
        # block that declares an operation but no classification is
        # honestly unclassified, and POL-003 fires as it should.
        source=(
            ClassificationSource.explicit if values is not None
            else ClassificationSource.unclassified
        ),
    )

    unknown = unknown_keys(raw)
    if not unknown:
        return classification, None

    # Rev 6: report, keep what validates, infer nothing.
    return classification, _partial_reason(unknown, raw)


def _partial_reason(unknown: Tuple[str, ...], block: Mapping) -> str:
    """Say what was not understood AND what survived, by field name.

    Without the second half a developer cannot tell whether their
    classification was kept, and partial acceptance would be quieter in
    practice than rejection despite emitting the same error.
    """
    named = ", ".join(f"'{key}'" for key in unknown)
    retained = [f"'{key}'" for key in sorted(_RECOGNISED) if key in block]
    fell_back = [f"'{key}'" for key in sorted(_RECOGNISED) if key not in block]

    parts = [
        f"unrecognised {'key' if len(unknown) == 1 else 'keys'} {named} "
        f"ignored, and not mapped to any recognised field"
    ]
    parts.append(
        f"retained {', '.join(retained)}" if retained
        else "no recognised field was supplied"
    )
    if fell_back:
        parts.append(f"{', '.join(fell_back)} fell back to the default")
    return "; ".join(parts)
