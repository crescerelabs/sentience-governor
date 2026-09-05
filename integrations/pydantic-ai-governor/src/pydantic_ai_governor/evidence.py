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


# --- CP6: token and model evidence -------------------------------------
#
# Every value below is read from the `ModelResponse` the model just
# returned. **`ctx.usage` is never a source here.** Measured at
# pydantic-ai 2.37.0, `ctx.usage` lags by one request at
# `after_model_request`: across two turns using 10/5 then 30/7 it reads
# 0/0 then 10/5. A delta taken from it credits each turn with the
# previous turn's tokens and never attributes the last one at all.

#: Provenance labels for where `llm_turn_id` came from. Core's
#: `provenance` is an open-valued `List[str]` (`schema/events.py:221`)
#: whose convention is a lowercase source-kind label naming the origin of
#: the recorded material — `"tool_output"` and `"claude_code_transcript"`
#: are shipped examples (`wrapper/langchain_adapter.py:635`,
#: `wrapper/claude_code_hook.py:984`). These follow that convention; they
#: do not introduce a new one.
PROVIDER_TURN_ID = "provider_response_id"
LOCAL_TURN_ID = "pydantic_ai_governor_run_step"


@dataclass(frozen=True)
class TurnEvidence:
    """One model turn's measured usage and identity.

    Everything optional here is optional *because core omits None and
    preserves 0* (`ContextSnapshotPayload.serialize_with_token_omission`).
    That distinction is load-bearing: absence means the framework reported
    nothing, and zero means it reported none. Neither is manufactured from
    the other.
    """

    llm_turn_id: str
    context_size_tokens: int
    provenance: List[str]
    llm_prompt_tokens: Optional[int] = None
    llm_completion_tokens: Optional[int] = None
    llm_cached_read_tokens: Optional[int] = None
    llm_cached_write_tokens: Optional[int] = None
    model_identifier: Optional[str] = None
    provider: Optional[str] = None
    tool_use_ids: Optional[List[str]] = None

    @property
    def turn_id_is_provider_issued(self) -> bool:
        return PROVIDER_TURN_ID in self.provenance


def turn_identity(response: Any, run_step: Any) -> Tuple[str, List[str]]:
    """This turn's `llm_turn_id` and the provenance that explains it.

    Prefers the provider's own `provider_response_id`. When the provider
    issued none, falls back to a local ``run_step:<n>`` built from
    ``ctx.run_step``.

    **The fallback is never presented as provider-issued.** Which of the
    two happened is recorded positively in `provenance`, and *both*
    branches are labelled rather than only the local one: if absence were
    the signal for "provider-issued", a dropped field or an older writer
    would read as a provider claim we never made.
    """
    provider_id = getattr(response, "provider_response_id", None)
    if isinstance(provider_id, str) and provider_id:
        return provider_id, [PROVIDER_TURN_ID]
    step = run_step if isinstance(run_step, int) else 0
    return f"run_step:{step}", [LOCAL_TURN_ID]


def issued_tool_use_ids(response: Any) -> Optional[List[str]]:
    """The tool-call ids this response issued, in response order.

    This is the join `build_token_snapshot` exists to carry: its contract
    says the snapshot records "the ``tool_use_ids`` that turn issued" and
    that analyzers join live tool-call violations against them
    (`event_builder/builder.py:487-497`). **Core's field is the join
    mechanism; the integration does not build a second one.**

    Ids are read off the parts and never inferred or synthesized. A turn
    that issued no tool calls returns ``None``, which is core's absence
    representation — the serializer omits None and preserves 0/empty, so
    an empty list would instead assert "this turn issued exactly zero
    tools" on every text-only turn.
    """
    ids: List[str] = []
    for part in getattr(response, "parts", None) or []:
        if getattr(part, "part_kind", None) != "tool-call":
            continue
        call_id = getattr(part, "tool_call_id", None)
        if isinstance(call_id, str) and call_id:
            ids.append(call_id)
    return ids or None


def _reported(usage: Any, name: str) -> Optional[int]:
    """A measured count, or None when the framework reported nothing.

    Never estimated, derived or recomputed. A reported ``0`` passes
    through as ``0`` because core preserves zero as a real measurement; a
    missing attribute becomes None so core omits the field entirely.
    """
    value = getattr(usage, name, None)
    return value if isinstance(value, int) else None


def read_turn(response: Any, run_step: Any = None) -> TurnEvidence:
    """Everything CP6 records about one model turn.

    `context_size_tokens` is the **measured** ``usage.input_tokens``, not
    the CP5 estimator: the field is required and non-nullable
    (`schema/events.py:223`), and where the provider reported the actual
    input size an estimate would be strictly less truthful.

    **`llm_prompt_tokens` deliberately carries the same number.** They are
    one measurement seen through two names, and inventing a different
    estimate to make them differ would put a false reading on the event.

    When the framework reported no usage at all, `context_size_tokens`
    falls back to ``0`` only because the field cannot be omitted. The
    optional token fields go absent in that case, so the two together say
    "nothing was reported" rather than "zero tokens were used". Estimating
    a substitute here is not an option the plan allows.
    """
    usage = getattr(response, "usage", None)
    turn_id, provenance = turn_identity(response, run_step)
    input_tokens = _reported(usage, "input_tokens")
    return TurnEvidence(
        llm_turn_id=turn_id,
        context_size_tokens=input_tokens if input_tokens is not None else 0,
        provenance=provenance,
        llm_prompt_tokens=input_tokens,
        llm_completion_tokens=_reported(usage, "output_tokens"),
        llm_cached_read_tokens=_reported(usage, "cache_read_tokens"),
        llm_cached_write_tokens=_reported(usage, "cache_write_tokens"),
        model_identifier=getattr(response, "model_name", None),
        provider=getattr(response, "provider_name", None),
        tool_use_ids=issued_tool_use_ids(response),
    )
