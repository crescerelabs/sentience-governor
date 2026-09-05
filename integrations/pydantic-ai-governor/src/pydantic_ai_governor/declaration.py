"""Reading the run declaration out of Pydantic AI run metadata (CP3).

The contract a developer writes::

    await agent.run(
        "...",
        metadata={"sentience_governor": {
            "objective": "Reconcile August invoices",
            "scope": ["crm", "billing"],
        }},
    )

Constructor values are defaults; a per-run block overrides them.

**Malformed input is never silently ignored.** A developer who writes
governance metadata believes they declared something, and if it were
discarded quietly their belief and the evidence would disagree with
nothing to tell them. See ``validate`` and the module's callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

NAMESPACE = "sentience_governor"

_OBJECTIVE = "objective"
_SCOPE = "scope"

# The complete set of keys this contract recognises. Anything else inside an
# explicitly present block is malformed (Rev 5), not forward compatibility:
# a developer who writes `objectiv` has declared nothing, and silence there
# is exactly the failure visible fail-open exists to prevent.
_RECOGNISED = frozenset({_OBJECTIVE, _SCOPE})


@dataclass(frozen=True)
class Declaration:
    """What this run says it is for.

    ``objective`` is None when nothing was declared from either source.
    That is a real state, not a placeholder, and it maps onto the core
    schema's ``IntentSource.none``.
    """

    objective: Optional[str] = None
    scope: Tuple[str, ...] = ()

    @property
    def is_declared(self) -> bool:
        return bool(self.objective)


def validate(block: Any) -> Optional[str]:
    """Describe the first contract problem with a metadata block, or None.

    The returned string names **the field and the contract it broke** and
    never reproduces a value. A rejected declaration is exactly where a
    developer might have put something sensitive, and both the warning and
    the emitted error leave this process.

    **An unrecognised key is a problem** (Rev 5). The key is named so the
    developer can find the typo; its value never is.
    """
    if not isinstance(block, Mapping):
        return (
            f"the '{NAMESPACE}' run metadata must be a mapping of "
            f"'{_OBJECTIVE}' and '{_SCOPE}'"
        )

    if _OBJECTIVE in block:
        objective = block[_OBJECTIVE]
        if not isinstance(objective, str) or not objective.strip():
            return f"'{_OBJECTIVE}' must be a non-empty string"

    if _SCOPE in block:
        scope = block[_SCOPE]
        if isinstance(scope, (str, bytes)) or not isinstance(scope, Sequence):
            return f"'{_SCOPE}' must be a list of strings"
        for item in scope:
            if not isinstance(item, str) or not item.strip():
                return f"'{_SCOPE}' must contain only non-empty strings"

    unknown = sorted(str(key) for key in block if key not in _RECOGNISED)
    if unknown:
        named = ", ".join(f"'{key}'" for key in unknown)
        recognised = ", ".join(f"'{key}'" for key in sorted(_RECOGNISED))
        return (
            f"unrecognised {'key' if len(unknown) == 1 else 'keys'} {named}; "
            f"the '{NAMESPACE}' block accepts only {recognised}"
        )

    return None


def resolve(
    metadata: Any, default: Declaration
) -> Tuple[Declaration, Optional[str]]:
    """The declaration in force for this run, and any rejection reason.

    Three outcomes:

    * **No block.** Not an error. The constructor defaults stand, and the
      caller emits nothing extra.
    * **A valid block.** Its keys override the defaults; keys it omits fall
      back to them.
    * **A malformed block.** Rejected **as a unit** so a good objective
      beside a broken scope never yields a half-declaration presented as
      explicit. The defaults remain in force and the reason says so, so a
      developer can tell which declaration actually governs the run.
    """
    raw = None
    if isinstance(metadata, Mapping):
        raw = metadata.get(NAMESPACE)

    if raw is None:
        return default, None

    problem = validate(raw)
    if problem is not None:
        return default, f"{problem}; {_in_force(default)}"

    objective = raw.get(_OBJECTIVE, default.objective)
    scope = raw[_SCOPE] if _SCOPE in raw else default.scope
    return Declaration(objective=objective, scope=tuple(scope)), None


def _in_force(default: Declaration) -> str:
    """What governs the run after a rejection. Never echoes a value."""
    if default.is_declared:
        return (
            "the declaration rejected, the objective and scope supplied at "
            "construction remain in force for this run"
        )
    return (
        "the declaration rejected, and no declaration was supplied at "
        "construction, so this run is undeclared"
    )
