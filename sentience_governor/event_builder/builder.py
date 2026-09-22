"""EventBuilder — the ONLY module that may compute advisory_flags,
policy_violations, and simulated_consequence.

Policy evaluation order (deterministic, fixed):
  REGISTRATION → INTENT → SCOPE → CONTEXT → MEMORY

First violation in this order determines simulated_consequence.

Lock usage
----------
EventBuilder uses the per-session sequencing lock owned by SessionManager.
It does NOT own or manage the lock's lifecycle.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import ValidationError

from sentience_governor.cache.cache import (
    SENSITIVITY_TIERS,
    InProcessCache,
    _NO_PRIOR,
    max_sensitivity_tier,
)
from sentience_governor.schema.events import (
    AdvisoryFlag,
    AgentRegisteredPayload,
    ClassificationSource,
    ContextSnapshotPayload,
    DeploymentMode,
    ErrorType,
    EventType,
    GovernanceErrorPayload,
    GovernanceEvent,
    IntentConfidence,
    IntentDeclaredPayload,
    IntentSource,
    InterceptStage,
    MemoryWriteAttemptPayload,
    OperationClassification,
    OperationType,
    PolicyViolation,
    PrimitiveType,
    ScopeAssertedPayload,
    Severity,
)
from sentience_governor.profile.schema import (
    DEMAND_AT_FIRST_WRITE,
    DEMAND_AT_NEVER,
    DEMAND_AT_SESSION_START,
    OPERATION_ACTIONS,
    OPERATION_DOMAINS,
    OPERATION_RULE_KEYS,
    SCHEMA_VERSION,
    SECTION_HIGH_CONSEQUENCE,
    SECTION_SESSION_INTENT,
    SECTION_TASK_BOUNDARY,
    SIGNAL_DIR_CHANGE,
    SIGNAL_FILE_TYPE_SHIFT,
    SIGNAL_READ_TO_WRITE_TRANSITION,
    SIGNAL_TIME_GAP,
)
from sentience_governor.session_manager.manager import SessionManager

logger = logging.getLogger(__name__)

# v0.3.2: the resolver outcomes whose provenance is recorded on
# AGENT_REGISTERED (resolver.SOURCE_BOUND / SOURCE_DEGRADED). "default"
# and "none" are omitted so those registrations stay byte-identical to
# v0.3.1.2. Literal strings here keep this module free of a resolver
# import; the resolver's constants are pinned equal by test.
PROVENANCE_RECORDED_RESOLUTIONS = frozenset(["bound", "degraded"])

# ---------------------------------------------------------------------------
# Policy: simulated_consequence strings (verbatim from spec / golden trace)
# ---------------------------------------------------------------------------
_CONSEQUENCE: Dict[str, str] = {
    PolicyViolation.POL_001: (
        "This WRITE operation would have been blocked. "
        "The agent must declare intent before executing mutating operations."
    ),
    PolicyViolation.POL_002: (
        "This session would have been blocked at the central server. "
        "Unregistered agents cannot access tools."
    ),
    PolicyViolation.POL_003: (
        "Downstream tool calls requiring classified context would have been "
        "restricted until classification was provided."
    ),
    PolicyViolation.POL_004: (
        "This write would have been blocked. "
        "Memory writes require both classification and a retention policy."
    ),
    PolicyViolation.POL_005: (
        "This context escalation would have triggered a boundary check. "
        "The session would have been paused pending authorization review."
    ),
}

# F15 (v0.2.8.1): POL-001 also fires on READ operations (via
# SCOPE_INTENT_MISMATCH), where the default WRITE wording above is wrong.
# READ-specific variant; the mutating string stays verbatim from spec/golden.
_CONSEQUENCE_POL_001_READ = (
    "This READ operation would have been flagged. "
    "The agent must declare intent before operating."
)

# Primitive evaluation order used to pick first consequence
_PRIMITIVE_ORDER = [
    PrimitiveType.REGISTRATION,
    PrimitiveType.INTENT,
    PrimitiveType.SCOPE,
    PrimitiveType.CONTEXT,
    PrimitiveType.MEMORY,
]

# Mapping from PolicyViolation → its primitive (for ordering)
_VIOLATION_PRIMITIVE: Dict[str, PrimitiveType] = {
    PolicyViolation.POL_002: PrimitiveType.REGISTRATION,
    PolicyViolation.POL_001: PrimitiveType.SCOPE,   # fires at SCOPE or INTENT (SCOPE wins order)
    PolicyViolation.POL_003: PrimitiveType.CONTEXT,
    PolicyViolation.POL_004: PrimitiveType.MEMORY,
    PolicyViolation.POL_005: PrimitiveType.CONTEXT,
}


def _first_consequence(
    violations: List[str],
    operation_type: Optional[Any] = None,
) -> Optional[str]:
    """Return simulated_consequence for the first violation in evaluation order.

    F15 (v0.2.8.1): POL-001 also fires on READ operations (via
    SCOPE_INTENT_MISMATCH), where the default WRITE wording is wrong. When the
    first consequence is POL-001 on a READ, return the READ variant; mutating
    operations (and unknown operation_type) keep the spec/golden WRITE string
    verbatim.
    """
    if not violations:
        return None
    ordered = sorted(
        violations,
        key=lambda v: _PRIMITIVE_ORDER.index(
            _VIOLATION_PRIMITIVE.get(v, PrimitiveType.SYSTEM)
        ),
    )
    first = ordered[0]
    if first == PolicyViolation.POL_001:
        op_val = getattr(operation_type, "value", operation_type)
        if op_val == OperationType.READ.value:
            return _CONSEQUENCE_POL_001_READ
    return _CONSEQUENCE.get(first)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
           f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# v0.2.5 — task-boundary signal helpers (pure functions)
# ---------------------------------------------------------------------------

_SHELL_NAMESPACE = "shell"


def _is_shell_target(target_system: str) -> bool:
    return target_system == _SHELL_NAMESPACE or target_system.startswith(_SHELL_NAMESPACE + "/")


def _valid_operation_rule(rule: object) -> bool:
    """Runtime mirror of the loader's ``high_consequence.operations``
    validation: a malformed rule is skipped, never raised on."""
    if not isinstance(rule, dict):
        return False
    for key in rule:
        if key not in OPERATION_RULE_KEYS:
            return False
    for key, vocabulary in (("domain", OPERATION_DOMAINS), ("action", OPERATION_ACTIONS)):
        if key in rule:
            raw = rule[key]
            members = raw if isinstance(raw, list) else [raw]
            if not members:
                return False
            if any(not isinstance(m, str) or m not in vocabulary for m in members):
                return False
    if "destructive" in rule and not isinstance(rule["destructive"], bool):
        return False
    return True


def _operation_rule_matches(rule: dict, classification: OperationClassification) -> bool:
    """A rule matches iff at least ONE single ClassifiedEffect, in any
    segment, satisfies EVERY predicate the rule specifies.

    ``domain`` / ``action`` values or lists are sets. ``destructive: true``
    is satisfied only by ``True`` and ``false`` only by ``False``; a ``None``
    effect satisfies neither. Predicates are never combined across
    effects: the domain of one effect and the destructiveness of another
    can never together satisfy a rule. A rule with no predicates matches
    any effect (validation warns about it).
    """
    def as_set(v):
        return set(v) if isinstance(v, list) else {v}

    for segment in classification.segments:
        for effect in segment.effects:
            if "domain" in rule and effect.domain not in as_set(rule["domain"]):
                continue
            if "action" in rule and effect.action not in as_set(rule["action"]):
                continue
            if "destructive" in rule and effect.destructive is not rule["destructive"]:
                continue
            return True
    return False


def _extract_dir(target_system: str, depth: int) -> Optional[str]:
    """Return a directory key for a target_system string at the given depth.

    Splits on ``/`` and ``.`` (covers filesystem paths and dotted
    identifiers like ``sentience_governor.profile.loader``). Returns
    the first ``depth`` components joined by ``/``. Used to compare
    "is this in the same logical directory as the prior event?"

    Returns None for empty input. Returns the whole string when depth
    exceeds the number of components.
    """
    if not target_system:
        return None
    if _is_shell_target(target_system):
        # v0.3.2 namespace guard: every Bash target (`shell`,
        # `shell/<domain>`) is the single `shell` namespace. A change of
        # semantic subtype (`shell/version_control` → `shell/filesystem`)
        # is not a directory move; Bash → Edit still crosses as before.
        return _SHELL_NAMESPACE
    parts = re.split(r"[/.]", target_system)
    # Strip empty parts (leading slash, trailing slash)
    parts = [p for p in parts if p]
    if not parts:
        return None
    if depth <= 0:
        return None
    return "/".join(parts[:depth])


def _extract_file_ext(target_system: str) -> Optional[str]:
    """Return the file extension portion of a target_system string.

    For ``foo/bar.py`` returns ``py``. For ``foo/bar`` (no extension)
    returns None. Used to detect file_type_shift signal.
    """
    if not target_system:
        return None
    if _is_shell_target(target_system):
        return None  # v0.3.2 namespace guard: a domain token is never an extension
    # Last path component (handle slashes)
    last = target_system.rsplit("/", 1)[-1]
    if "." not in last:
        return None
    ext = last.rsplit(".", 1)[-1]
    return ext or None


def _detect_task_boundary(
    *,
    signals: List[str],
    prior_state: Any,  # SessionCacheEntry or None
    current_target_system: str,
    current_operation_type: OperationType,
    current_monotonic: float,
    time_gap_seconds: int,
    dir_change_depth: int,
) -> bool:
    """True iff any active signal indicates a task boundary on this event.

    Returns False on the first SCOPE_ASSERTED of a session (no prior
    state to compare against). For subsequent events, evaluates each
    active signal independently and returns True if any fire.

    Pure function over its arguments; no I/O or side effects.
    """
    # First event of session — no baseline to compare. Cannot cross
    # a boundary from nothing.
    if prior_state is None or prior_state.last_scope_activity_monotonic is None:
        return False

    if SIGNAL_DIR_CHANGE in signals:
        current_dir = _extract_dir(current_target_system, dir_change_depth)
        if (
            current_dir is not None
            and prior_state.last_target_dir is not None
            and current_dir != prior_state.last_target_dir
        ):
            return True

    if SIGNAL_FILE_TYPE_SHIFT in signals:
        current_ext = _extract_file_ext(current_target_system)
        prior_ext = prior_state.last_file_ext
        # Only a shift when both sides have an extension and they differ.
        # Going from "no ext" to "ext" or vice versa is NOT a shift
        # under v0.2.5 (avoids false positives on tool-name mismatches).
        if (
            current_ext is not None
            and prior_ext is not None
            and current_ext != prior_ext
        ):
            return True

    if SIGNAL_TIME_GAP in signals:
        gap = current_monotonic - prior_state.last_scope_activity_monotonic
        if gap >= time_gap_seconds:
            return True

    if SIGNAL_READ_TO_WRITE_TRANSITION in signals:
        prior_op = prior_state.last_operation_type
        current_op = current_operation_type.value
        if prior_op == OperationType.READ.value and current_op in (
            OperationType.WRITE.value,
            OperationType.DELETE.value,
            OperationType.EXECUTE.value,
        ):
            return True

    return False


class _ProfileView:
    """The checked, per-session reading of a profile (v0.3.2.1, see
    ``EventBuilder._checked_profile``). Plain attributes; built once."""

    __slots__ = (
        "schema_version",
        "session_intent",
        "task_boundary",
        "high_consequence",
        "signals",
        "time_gap_seconds",
        "dir_change_depth",
        "tools",
    )

    def __init__(
        self,
        *,
        schema_version: Optional[int],
        session_intent: Dict[str, Any],
        task_boundary: Dict[str, Any],
        high_consequence: Dict[str, Any],
    ) -> None:
        self.schema_version = schema_version
        self.session_intent = session_intent
        self.task_boundary = task_boundary
        self.high_consequence = high_consequence
        self.signals: List[str] = []
        self.time_gap_seconds: Any = 300
        self.dir_change_depth: Any = 2
        self.tools: List[str] = []


class EventBuilder:
    """Constructs and validates all governance events.

    Parameters
    ----------
    session_manager : SessionManager
        Provides per-session sequencing lock and sequence number management.
    cache : InProcessCache
        Provides intent baseline and context sensitivity tier lookups.
    agent_id : str
        Agent identifier used for all events in this builder instance.
    session_id : str
        Session identifier.
    deployment_mode : DeploymentMode
        Topology declared at session start.
    """

    def __init__(
        self,
        session_manager: SessionManager,
        cache: InProcessCache,
        agent_id: str,
        session_id: str,
        deployment_mode: DeploymentMode = DeploymentMode.vendor_managed,
        profile_resolution: Optional[str] = None,
        profile_binding: Optional[str] = None,
    ) -> None:
        self._sm = session_manager
        self._cache = cache
        self._agent_id = agent_id
        self._session_id = session_id
        self._deployment_mode = deployment_mode
        # v0.3.2: how the session's profile was resolved (the resolver's
        # ``ResolvedProfile.source`` and ``.binding``). Recorded on
        # AGENT_REGISTERED only for "bound" and "degraded"; "default" and
        # "none" (and callers that pass nothing) leave the registration
        # exactly as v0.3.1.2 wrote it.
        self._profile_resolution = profile_resolution
        self._profile_binding = profile_binding
        # v0.3.2.1: the checked profile view (see _checked_profile). Built
        # once per session from the raw profile data; every consumer reads
        # it instead of the profile's accessors. Keyed on the profile
        # object so a session whose profile is replaced (a test seam; the
        # runtime never does it) is re-checked rather than served stale.
        self._profile_view: Optional[_ProfileView] = None
        self._profile_view_source: Optional[object] = None
        self._profile_warned: set = set()

    # ------------------------------------------------------------------
    # v0.3.2.1: checked profile view (runtime tolerance)
    # ------------------------------------------------------------------

    def _checked_profile(self, profile: object) -> "_ProfileView":
        """Return the once-per-session checked view of ``profile``.

        The validator (``GovernanceProfile.validate``) is the definition of
        an invalid profile and the binding path refuses one before it gets
        here. This is the defensive layer behind that: if a malformed
        profile does arrive (a direct caller bypassing ``session_start``,
        or a shape the validator does not know), each consumer substitutes
        the default the validator would have insisted on, never a value
        derived from the malformed one, and warns once per session per
        field. No event or flag is added.

        Raw values come from ``to_dict()``. The accessors cannot be used
        for the checks: ``schema_version`` applies ``int()`` (so ``true``
        becomes ``1``) and the section accessors apply ``dict()`` (so a
        list of pairs becomes a mapping), which would turn an invalid
        shape into policy.
        """
        if self._profile_view is not None and self._profile_view_source is profile:
            return self._profile_view
        try:
            raw = profile.to_dict()  # type: ignore[attr-defined]
        except Exception as exc:  # not a GovernanceProfile: govern from defaults
            self._warn_profile_field("profile", f"could not be read ({exc.__class__.__name__}: {exc})", "no profile transforms")
            raw = {}
        if not isinstance(raw, dict):
            self._warn_profile_field("profile", f"is {type(raw).__name__}, not a mapping", "no profile transforms")
            raw = {}
        view = _ProfileView(
            schema_version=self._checked_schema_version(raw),
            session_intent=self._checked_section(raw, SECTION_SESSION_INTENT),
            task_boundary=self._checked_section(raw, SECTION_TASK_BOUNDARY),
            high_consequence=self._checked_section(raw, SECTION_HIGH_CONSEQUENCE),
        )
        view.signals = self._checked_signals(view.task_boundary)
        view.time_gap_seconds = self._checked_number(
            view.task_boundary, "time_gap_seconds", default=300, integer_only=False, minimum=0
        )
        view.dir_change_depth = self._checked_number(
            view.task_boundary, "dir_change_depth", default=2, integer_only=True, minimum=1
        )
        view.tools = self._checked_tools(view.high_consequence)
        self._profile_view = view
        self._profile_view_source = profile
        return view

    def _checked_schema_version(self, raw: Dict[str, Any]) -> Optional[int]:
        value = raw.get("schema_version", SCHEMA_VERSION)
        if isinstance(value, bool) or not isinstance(value, int):
            self._warn_profile_field("schema_version", f"is {type(value).__name__}, not an integer", "not recorded")
            return None
        return value

    def _checked_section(self, raw: Dict[str, Any], name: str) -> Dict[str, Any]:
        value = raw.get(name)
        if value is None:
            return {}
        if not isinstance(value, dict):
            self._warn_profile_field(name, f"is {type(value).__name__}, not a mapping", "section ignored")
            return {}
        return value

    def _checked_signals(self, task_boundary: Dict[str, Any]) -> List[str]:
        value = task_boundary.get("signals")
        if value is None:
            return []
        if not isinstance(value, list):
            self._warn_profile_field(f"{SECTION_TASK_BOUNDARY}.signals", f"is {type(value).__name__}, not a list", "no boundary detection")
            return []
        kept = [s for s in value if isinstance(s, str)]
        if len(kept) != len(value):
            self._warn_profile_field(f"{SECTION_TASK_BOUNDARY}.signals", f"has {len(value) - len(kept)} non-string entries", "those entries ignored")
        return kept

    def _checked_number(
        self,
        task_boundary: Dict[str, Any],
        key: str,
        *,
        default: int,
        integer_only: bool,
        minimum: int,
    ) -> Any:
        value = task_boundary.get(key)
        if value is None:
            return default
        ok = not isinstance(value, bool)
        if ok:
            ok = isinstance(value, int) if integer_only else isinstance(value, (int, float))
        if ok and isinstance(value, float):
            ok = math.isfinite(value)
        if ok:
            ok = value >= minimum
        if not ok:
            self._warn_profile_field(f"{SECTION_TASK_BOUNDARY}.{key}", f"is {value!r}", f"using {default}")
            return default
        return value

    def _checked_tools(self, high_consequence: Dict[str, Any]) -> List[str]:
        value = high_consequence.get("tools")
        if value is None:
            return []
        if not isinstance(value, list):
            self._warn_profile_field(f"{SECTION_HIGH_CONSEQUENCE}.tools", f"is {type(value).__name__}, not a list", "no tool patterns")
            return []
        kept = [p for p in value if isinstance(p, str)]
        if len(kept) != len(value):
            self._warn_profile_field(f"{SECTION_HIGH_CONSEQUENCE}.tools", f"has {len(value) - len(kept)} non-string entries", "those entries skipped")
        return kept

    def _warn_profile_field(self, field: str, problem: str, substitution: str) -> None:
        """Once per session per field: the profile reached the runtime malformed."""
        if field in self._profile_warned:
            return
        self._profile_warned.add(field)
        logger.warning(
            "PROFILE_FIELD_IGNORED: session %s: profile field %s %s; %s",
            self._session_id,
            field,
            problem,
            substitution,
        )

    # ------------------------------------------------------------------
    # Public factory methods (one per event type)
    # ------------------------------------------------------------------

    def build_agent_registered(
        self,
        agent_version: Optional[str],
        vendor_id: Optional[str],
        declared_capabilities: List[str],
        owner_claim: Optional[str],
        policy_context: Optional[str] = None,
        timestamp_utc: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> Optional[GovernanceEvent]:
        # v0.2.5: AGENT_REGISTERED is the trace's pinned correlation
        # surface for profile metadata. profile_loaded is True only
        # when an operator-authored file backs the profile (source_path
        # set); defaults-only profiles do NOT set profile_loaded, so
        # no-profile sessions produce byte-identical v0.2.4 events.
        profile = self._sm.get_profile(self._session_id)
        profile_loaded: Optional[bool] = None
        profile_schema_version: Optional[int] = None
        if profile is not None and getattr(profile, "source_path", None) is not None:
            profile_loaded = True
            # v0.3.2.1: the raw value, checked; a boolean or fractional
            # schema_version is recorded as absent, not coerced to 1.
            profile_schema_version = self._checked_profile(profile).schema_version

        # v0.3.2: resolution provenance, recorded for bound/degraded only.
        profile_resolution: Optional[str] = None
        profile_binding: Optional[str] = None
        if self._profile_resolution in PROVENANCE_RECORDED_RESOLUTIONS:
            profile_resolution = self._profile_resolution
            profile_binding = self._profile_binding

        payload = AgentRegisteredPayload(
            agent_id=self._agent_id,
            agent_version=agent_version,
            vendor_id=vendor_id,
            deployment_mode=self._deployment_mode,
            declared_capabilities=declared_capabilities,
            owner_claim=owner_claim,
            policy_context=policy_context,
            profile_loaded=profile_loaded,
            profile_schema_version=profile_schema_version,
            profile_resolution=profile_resolution,
            profile_binding=profile_binding,
        )
        flags, violations = self._eval_registration(payload)
        return self._finalise(
            event_id=event_id,
            event_type=EventType.AGENT_REGISTERED,
            primitive=PrimitiveType.REGISTRATION,
            payload=payload,
            flags=flags,
            violations=violations,
            timestamp_utc=timestamp_utc,
        )

    def build_intent_declared(
        self,
        stated_objective: Optional[str],
        intent_source: IntentSource,
        intent_confidence: IntentConfidence,
        authorization_claim: Optional[str],
        session_scope_hint: List[str],
        timestamp_utc: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> Optional[GovernanceEvent]:
        payload = IntentDeclaredPayload(
            stated_objective=stated_objective,
            intent_source=intent_source,
            intent_confidence=intent_confidence,
            authorization_claim=authorization_claim,
            session_scope_hint=session_scope_hint,
        )
        flags, violations = self._eval_intent(payload)
        # Update cache — intent baseline set from first INTENT_DECLARED
        self._cache.set_intent_baseline(
            self._session_id,
            stated_objective=stated_objective,
            scope_hint=session_scope_hint,
        )
        return self._finalise(
            event_id=event_id,
            event_type=EventType.INTENT_DECLARED,
            primitive=PrimitiveType.INTENT,
            payload=payload,
            flags=flags,
            violations=violations,
            timestamp_utc=timestamp_utc,
        )

    def build_scope_asserted(
        self,
        tool_id: str,
        asserted_permissions: List[str],
        target_system: str,
        operation_type: OperationType,
        authorization_claim: Optional[str] = None,
        timestamp_utc: Optional[str] = None,
        event_id: Optional[str] = None,
        tool_use_id: Optional[str] = None,
        operation_classification: Optional[OperationClassification] = None,
    ) -> Optional[GovernanceEvent]:
        # v0.3.2: the semantic classification of a shell command, when the
        # adapter classified one (Claude Code Bash calls). Optional and
        # None-omitted; `operation_type` and `target_system` are supplied
        # by the caller exactly as before.
        payload = ScopeAssertedPayload(
            tool_id=tool_id,
            asserted_permissions=asserted_permissions,
            target_system=target_system,
            operation_type=operation_type,
            tool_use_id=tool_use_id,
            operation_classification=operation_classification,
        )
        flags, violations = self._eval_scope(payload, authorization_claim)
        # v0.2.5: apply profile-driven transforms (POL-001 gating,
        # task-boundary signals, high-consequence patterns). No-op when
        # the session has no profile attached.
        profile = self._sm.get_profile(self._session_id)
        if profile is not None:
            flags, violations = self._apply_profile_to_scope(
                profile=profile,
                payload=payload,
                flags=flags,
                violations=violations,
            )
        return self._finalise(
            event_id=event_id,
            event_type=EventType.SCOPE_ASSERTED,
            primitive=PrimitiveType.SCOPE,
            payload=payload,
            flags=flags,
            violations=violations,
            timestamp_utc=timestamp_utc,
        )

    def build_context_snapshot(
        self,
        data_classifications: List[str],
        classification_source: ClassificationSource,
        provenance: List[str],
        retention_flags: List[str],
        context_size_tokens: int,
        authorization_claim: Optional[str] = None,
        timestamp_utc: Optional[str] = None,
        event_id: Optional[str] = None,
        # v0.2.3 Track 2 — LLM token accounting. All optional; None means
        # "not reported" and the field is omitted from the serialised
        # payload entirely. Zero is a real measurement and is preserved.
        llm_prompt_tokens: Optional[int] = None,
        llm_completion_tokens: Optional[int] = None,
        llm_cached_read_tokens: Optional[int] = None,
        llm_cached_write_tokens: Optional[int] = None,
        llm_reasoning_tokens: Optional[int] = None,
        model_identifier: Optional[str] = None,
        provider: Optional[str] = None,
        llm_turn_id: Optional[str] = None,
        # v0.2.6.1 — Claude Code join keys (None-omitted on serialize).
        tool_use_id: Optional[str] = None,
        tool_use_ids: Optional[List[str]] = None,
    ) -> Optional[GovernanceEvent]:
        payload = ContextSnapshotPayload(
            data_classifications=data_classifications,
            classification_source=classification_source,
            provenance=provenance,
            retention_flags=retention_flags,
            context_size_tokens=context_size_tokens,
            llm_prompt_tokens=llm_prompt_tokens,
            llm_completion_tokens=llm_completion_tokens,
            llm_cached_read_tokens=llm_cached_read_tokens,
            llm_cached_write_tokens=llm_cached_write_tokens,
            llm_reasoning_tokens=llm_reasoning_tokens,
            model_identifier=model_identifier,
            provider=provider,
            llm_turn_id=llm_turn_id,
            tool_use_id=tool_use_id,
            tool_use_ids=tool_use_ids,
        )
        # Update cache; get prior value (_NO_PRIOR sentinel on first snapshot)
        prior_value = self._cache.update_context_tier(
            self._session_id, data_classifications
        )
        flags, violations = self._eval_context(payload, prior_value, authorization_claim)
        return self._finalise(
            event_id=event_id,
            event_type=EventType.CONTEXT_SNAPSHOT,
            primitive=PrimitiveType.CONTEXT,
            payload=payload,
            flags=flags,
            violations=violations,
            timestamp_utc=timestamp_utc,
        )

    def build_token_snapshot(
        self,
        *,
        llm_turn_id: str,
        context_size_tokens: int,
        llm_prompt_tokens: Optional[int] = None,
        llm_completion_tokens: Optional[int] = None,
        llm_cached_read_tokens: Optional[int] = None,
        llm_cached_write_tokens: Optional[int] = None,
        llm_reasoning_tokens: Optional[int] = None,
        model_identifier: Optional[str] = None,
        provider: Optional[str] = None,
        tool_use_ids: Optional[List[str]] = None,
        provenance: Optional[List[str]] = None,
        timestamp_utc: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> Optional[GovernanceEvent]:
        """Append-only per-turn token-accounting CONTEXT_SNAPSHOT (v0.2.6.1).

        Semantically distinct from :meth:`build_context_snapshot`: it records a
        model turn's token usage (keyed by ``llm_turn_id`` = transcript
        ``requestId``) and the ``tool_use_ids`` that turn issued — it does NOT
        observe data classification. It therefore makes NO classification claim
        and is NOT run through ``_eval_context`` (running it would manufacture a
        spurious POL-003 on every turn, since the snapshot is "unclassified" by
        nature). It also does not perturb the context-sensitivity tier state
        machine. No flags, no violations — a pure token carrier the analyzers
        join live tool-call violations against via ``tool_use_id``.
        """
        payload = ContextSnapshotPayload(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=provenance or [],
            retention_flags=[],
            context_size_tokens=context_size_tokens,
            llm_prompt_tokens=llm_prompt_tokens,
            llm_completion_tokens=llm_completion_tokens,
            llm_cached_read_tokens=llm_cached_read_tokens,
            llm_cached_write_tokens=llm_cached_write_tokens,
            llm_reasoning_tokens=llm_reasoning_tokens,
            model_identifier=model_identifier,
            provider=provider,
            llm_turn_id=llm_turn_id,
            tool_use_ids=tool_use_ids,
        )
        return self._finalise(
            event_id=event_id,
            event_type=EventType.CONTEXT_SNAPSHOT,
            primitive=PrimitiveType.CONTEXT,
            payload=payload,
            flags=[],
            violations=[],
            timestamp_utc=timestamp_utc,
        )

    def build_memory_write_attempt(
        self,
        write_type,
        detection_mechanism,
        target_store: str,
        write_classification: str,
        write_size_tokens: int,
        retention_requested: Optional[str],
        timestamp_utc: Optional[str] = None,
        event_id: Optional[str] = None,
        tool_use_id: Optional[str] = None,
    ) -> Optional[GovernanceEvent]:
        payload = MemoryWriteAttemptPayload(
            write_type=write_type,
            detection_mechanism=detection_mechanism,
            target_store=target_store,
            write_classification=write_classification,
            write_size_tokens=write_size_tokens,
            retention_requested=retention_requested,
            tool_use_id=tool_use_id,
        )
        flags, violations = self._eval_memory(payload)
        return self._finalise(
            event_id=event_id,
            event_type=EventType.MEMORY_WRITE_ATTEMPT,
            primitive=PrimitiveType.MEMORY,
            payload=payload,
            flags=flags,
            violations=violations,
            timestamp_utc=timestamp_utc,
        )

    def build_governance_error(
        self,
        error_type: ErrorType,
        severity: Severity,
        failure_reason: str,
        intercept_stage: Optional[InterceptStage] = None,
        timestamp_utc: Optional[str] = None,
        event_id: Optional[str] = None,
    ) -> Optional[GovernanceEvent]:
        payload = GovernanceErrorPayload(
            error_type=error_type,
            severity=severity,
            intercept_stage=intercept_stage,
            failure_reason=failure_reason,
            agent_continued=True,
        )
        return self._finalise(
            event_id=event_id,
            event_type=EventType.GOVERNANCE_ERROR,
            primitive=PrimitiveType.SYSTEM,
            payload=payload,
            flags=[],
            violations=[],
            timestamp_utc=timestamp_utc,
        )

    # ------------------------------------------------------------------
    # Policy evaluation (private — ONLY place flags/violations are set)
    # ------------------------------------------------------------------

    def _eval_registration(
        self, payload: AgentRegisteredPayload
    ) -> Tuple[List[str], List[str]]:
        flags: List[str] = []
        violations: List[str] = []
        # AGENT_UNREGISTERED: no registration signal (agent_version null, vendor_id null,
        # declared_capabilities empty, owner_claim null)
        if (
            payload.agent_version is None
            and payload.vendor_id is None
            and not payload.declared_capabilities
            and payload.owner_claim is None
        ):
            flags.append(AdvisoryFlag.AGENT_UNREGISTERED)
            violations.append(PolicyViolation.POL_002)
        return flags, violations

    def _eval_intent(
        self, payload: IntentDeclaredPayload
    ) -> Tuple[List[str], List[str]]:
        flags: List[str] = []
        violations: List[str] = []
        if payload.intent_source == IntentSource.none:
            flags.append(AdvisoryFlag.INTENT_MISSING)
            # POL-001 fires on subsequent mutating operations, not here
        return flags, violations

    def _eval_scope(
        self,
        payload: ScopeAssertedPayload,
        authorization_claim: Optional[str],
    ) -> Tuple[List[str], List[str]]:
        flags: List[str] = []
        violations: List[str] = []

        intent_entry = self._cache.get_intent_baseline(self._session_id)

        is_mutating = payload.operation_type in (
            OperationType.WRITE,
            OperationType.DELETE,
            OperationType.EXECUTE,
        )

        # SCOPE_OPERATION_UNEXPECTED: mutating op with null intent baseline
        if is_mutating:
            if intent_entry is None or intent_entry.intent_stated_objective is None:
                flags.append(AdvisoryFlag.SCOPE_OPERATION_UNEXPECTED)
                violations.append(PolicyViolation.POL_001)

        # SCOPE_INTENT_MISMATCH: target_system not in scope hint
        if intent_entry is not None:
            scope_hint = [s.lower() for s in intent_entry.intent_scope_hint]
            target_lower = payload.target_system.lower()
            # Check if target system matches any hint (prefix or exact)
            match = any(
                target_lower == h or target_lower.startswith(h.split(".")[0])
                for h in scope_hint
            )
            if not match:
                flags.append(AdvisoryFlag.SCOPE_INTENT_MISMATCH)
                if PolicyViolation.POL_001 not in violations:
                    violations.append(PolicyViolation.POL_001)
        else:
            # No intent baseline yet → mismatch (baseline is null/empty)
            flags.append(AdvisoryFlag.SCOPE_INTENT_MISMATCH)
            if PolicyViolation.POL_001 not in violations:
                violations.append(PolicyViolation.POL_001)

        return flags, violations

    # ------------------------------------------------------------------
    # v0.2.5 — profile-driven scope transforms
    # ------------------------------------------------------------------

    def _apply_profile_to_scope(
        self,
        *,
        profile: object,
        payload: ScopeAssertedPayload,
        flags: List[str],
        violations: List[str],
    ) -> Tuple[List[str], List[str]]:
        """Apply profile-driven transforms to a freshly-evaluated SCOPE event.

        Three transforms run in fixed order:

        1. POL-001 gating per ``session_intent.demand_at``:
           * ``session_start`` (v0.2.4 default) — fire POL-001 every
             event without intent. No-op vs base eval.
           * ``first_write`` — fire POL-001 only on the first mutating
             event of the session. Subsequent POL-001 firings are
             suppressed; SCOPE_OPERATION_UNEXPECTED flag stays.
           * ``never`` — POL-001 never fires; suppress unconditionally.
        2. Task-boundary signal detection — when any signal is active
           and the prior-vs-current state crosses the signal, append
           ``TASK_BOUNDARY_CROSSED`` to advisory_flags. Multiple
           signals firing on one event still produce a single flag.
        3. High-consequence pattern match — when the
           ``tool_id:target_system`` composite matches any regex in
           ``high_consequence.tools``, append
           ``HIGH_CONSEQUENCE_DETECTED``.
        3b. (v0.3.2) High-consequence operations rules — when one single
           classified effect satisfies every predicate of a rule in
           ``high_consequence.operations``, append the same flag.

        After the transforms, the per-session task-boundary state is
        updated to reflect the current event (so the NEXT event sees
        the current as its prior).

        Returns ``(flags, violations)`` with profile transforms
        applied. Mutates a defensive copy of each input list, not the
        caller's list.
        """
        # Defensive copies — don't mutate caller's lists.
        out_flags = list(flags)
        out_violations = list(violations)

        # v0.3.2.1: read the once-per-session checked view, never the
        # accessors (see _checked_profile). A section that is not a
        # mapping is {} for the session; malformed parameters take the
        # validator's defaults; malformed collection entries are skipped.
        view = self._checked_profile(profile)
        session_intent = view.session_intent
        high_consequence = view.high_consequence

        # ---- Transform 1: POL-001 gating per demand_at ----
        demand_at = session_intent.get("demand_at", DEMAND_AT_SESSION_START)
        is_mutating = payload.operation_type in (
            OperationType.WRITE,
            OperationType.DELETE,
            OperationType.EXECUTE,
        )
        has_pol_001 = PolicyViolation.POL_001 in out_violations

        if has_pol_001:
            if demand_at == DEMAND_AT_NEVER:
                # Operator opted out — suppress POL-001 unconditionally.
                out_violations = [
                    v for v in out_violations if v != PolicyViolation.POL_001
                ]
            elif demand_at == DEMAND_AT_FIRST_WRITE and is_mutating:
                # Fire once per session; suppress on subsequent events.
                if self._cache.has_pol_001_fired(self._session_id):
                    out_violations = [
                        v for v in out_violations if v != PolicyViolation.POL_001
                    ]
                else:
                    self._cache.mark_pol_001_fired(self._session_id)
            elif demand_at == DEMAND_AT_SESSION_START:
                # v0.2.4 default — fire per-event; no transform needed.
                # Still record that POL-001 has fired (for analyzer use
                # if it ever wants to know).
                self._cache.mark_pol_001_fired(self._session_id)

        # ---- Transform 2: task-boundary signal detection ----
        signals = view.signals
        if signals:
            time_gap_seconds = view.time_gap_seconds
            dir_change_depth = view.dir_change_depth
            now_monotonic = time.monotonic()
            prior_state = self._cache.get_task_boundary_state(self._session_id)
            boundary_crossed = _detect_task_boundary(
                signals=signals,
                prior_state=prior_state,
                current_target_system=payload.target_system,
                current_operation_type=payload.operation_type,
                current_monotonic=now_monotonic,
                time_gap_seconds=time_gap_seconds,
                dir_change_depth=dir_change_depth,
            )
            if boundary_crossed and AdvisoryFlag.TASK_BOUNDARY_CROSSED not in out_flags:
                out_flags.append(AdvisoryFlag.TASK_BOUNDARY_CROSSED)

            # Update the cache state regardless of whether a boundary
            # fired — the *next* event needs the *current* as its prior.
            self._cache.update_task_boundary_state(
                self._session_id,
                target_system=payload.target_system,
                target_dir=_extract_dir(payload.target_system, dir_change_depth),
                file_ext=_extract_file_ext(payload.target_system),
                operation_type=payload.operation_type.value,
                activity_monotonic=now_monotonic,
            )

        # ---- Transform 3: high-consequence pattern match ----
        hc_tools = view.tools
        if hc_tools:
            composite = f"{payload.tool_id}:{payload.target_system}"
            for pattern in hc_tools:
                try:
                    if re.search(pattern, composite):
                        if AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED not in out_flags:
                            out_flags.append(AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED)
                        break
                except (re.error, TypeError):
                    # Pattern that does not compile: validation warns at
                    # load time; the runtime skips it, as it always has.
                    continue

        # ---- Transform 3b (v0.3.2): per-effect operations rules ----
        # Only when the event carries a classification. Each rule is
        # tested against one ClassifiedEffect at a time (never a
        # combination of fields from separate effects). A match raises
        # the same HIGH_CONSEQUENCE_DETECTED flag as the `tools` regex,
        # deduplicated with it; on_match stays "flag"; malformed rules
        # are skipped as validation promised.
        hc_ops = high_consequence.get("operations") or []
        classification = payload.operation_classification
        if hc_ops and classification is not None:
            for rule in hc_ops:
                if not _valid_operation_rule(rule):
                    continue
                try:
                    matched = _operation_rule_matches(rule, classification)
                except Exception:
                    continue
                if matched:
                    if AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED not in out_flags:
                        out_flags.append(AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED)
                    break

        return out_flags, out_violations

    def _eval_context(
        self,
        payload: ContextSnapshotPayload,
        prior_value: object,
        authorization_claim: Optional[str],
    ) -> Tuple[List[str], List[str]]:
        flags: List[str] = []
        violations: List[str] = []

        # CONTEXT_UNCLASSIFIED
        if payload.classification_source == ClassificationSource.unclassified:
            flags.append(AdvisoryFlag.CONTEXT_UNCLASSIFIED)
            violations.append(PolicyViolation.POL_003)

        # SENSITIVITY_ESCALATION: cannot fire on first snapshot (_NO_PRIOR sentinel)
        if prior_value is not _NO_PRIOR:
            # prior_value is the prior tier string (or None for unclassified/below-public)
            prior_tier: Optional[str] = prior_value  # type: ignore[assignment]
            current_max = max_sensitivity_tier(payload.data_classifications)
            # None (empty/unclassified) is treated as below "public" → index -1
            prior_idx = SENSITIVITY_TIERS.index(prior_tier) if prior_tier in SENSITIVITY_TIERS else -1
            current_idx = (
                SENSITIVITY_TIERS.index(current_max) if current_max in SENSITIVITY_TIERS else -1
            )
            if current_idx > prior_idx:
                flags.append(AdvisoryFlag.SENSITIVITY_ESCALATION)
                if authorization_claim is None:
                    violations.append(PolicyViolation.POL_005)

        return flags, violations

    def _eval_memory(
        self, payload: MemoryWriteAttemptPayload
    ) -> Tuple[List[str], List[str]]:
        flags: List[str] = []
        violations: List[str] = []

        # MEMORY_WRITE_CANDIDATE: write matched a known persistence target
        if payload.write_type.value == "write_to_persistence_target":
            flags.append(AdvisoryFlag.MEMORY_WRITE_CANDIDATE)

        # MEMORY_WRITE_UNCLASSIFIED
        if payload.write_classification == "unclassified":
            flags.append(AdvisoryFlag.MEMORY_WRITE_UNCLASSIFIED)

        # POL-004: unclassified OR retention_requested is null
        if payload.write_classification == "unclassified" or payload.retention_requested is None:
            violations.append(PolicyViolation.POL_004)

        return flags, violations

    # ------------------------------------------------------------------
    # Core finalisation — sequence assignment + schema validation
    # ------------------------------------------------------------------

    def _finalise(
        self,
        event_id: Optional[str],
        event_type: EventType,
        primitive: PrimitiveType,
        payload: Any,
        flags: List[str],
        violations: List[str],
        timestamp_utc: Optional[str],
    ) -> Optional[GovernanceEvent]:
        """Assign sequence, validate schema, return GovernanceEvent or None.

        On schema validation failure, emits GOVERNANCE_ERROR to stdout and
        returns None (caller must not pass None to SinkWriter).
        """
        eid = event_id or str(uuid.uuid4())
        ts = timestamp_utc or _now_utc()
        op_type = getattr(payload, "operation_type", None)
        consequence = _first_consequence(violations, operation_type=op_type)

        # v0.2.5: profile fingerprint on EVERY event (envelope-level
        # correlation key). Computed once per session and stable across
        # the session's lifetime because profiles are immutable per
        # session. None when no operator-authored profile is loaded —
        # the serializer omits the field, preserving v0.2.4 byte-shape.
        profile = self._sm.get_profile(self._session_id)
        profile_fingerprint: Optional[str] = None
        if profile is not None and getattr(profile, "source_path", None) is not None:
            profile_fingerprint = profile.fingerprint()  # type: ignore[attr-defined]

        with self._sm.acquire_sequence(self._session_id) as seq_ctx:
            seq = seq_ctx.next_sequence()
            prev = seq_ctx.last_event_id

            try:
                event = GovernanceEvent(
                    event_id=eid,
                    event_type=event_type,
                    session_id=self._session_id,
                    event_sequence_number=seq,
                    previous_event_id=prev,
                    agent_id=self._agent_id,
                    deployment_mode=self._deployment_mode,
                    timestamp_utc=ts,
                    primitive=primitive,
                    payload=payload,
                    advisory_flags=flags,
                    policy_violations=violations,
                    simulated_consequence=consequence,
                    pass_through=True,
                    profile_fingerprint=profile_fingerprint,
                )
            except (ValidationError, Exception) as exc:
                # Emit GOVERNANCE_ERROR to stdout; do not write failed event
                self._emit_schema_violation(str(exc), primitive)
                return None

            seq_ctx.set_last_event_id(eid)

        # Touch session inactivity timer
        self._sm.touch(self._session_id)
        return event

    def _emit_schema_violation(self, reason: str, stage: PrimitiveType) -> None:
        stage_map = {
            PrimitiveType.REGISTRATION: InterceptStage.REGISTRATION,
            PrimitiveType.INTENT: InterceptStage.INTENT,
            PrimitiveType.SCOPE: InterceptStage.SCOPE,
            PrimitiveType.CONTEXT: InterceptStage.CONTEXT,
            PrimitiveType.MEMORY: InterceptStage.MEMORY,
        }
        error_payload = GovernanceErrorPayload(
            error_type=ErrorType.SCHEMA_VIOLATION,
            severity=Severity.warning,
            intercept_stage=stage_map.get(stage),
            failure_reason=reason,
            agent_continued=True,
        )
        err_event = {
            "event_id": str(uuid.uuid4()),
            "event_type": EventType.GOVERNANCE_ERROR,
            "session_id": self._session_id,
            "agent_id": self._agent_id,
            "primitive": PrimitiveType.SYSTEM,
            "payload": error_payload.model_dump(),
            "advisory_flags": [],
            "policy_violations": [],
            "simulated_consequence": None,
            "pass_through": True,
        }
        print(json.dumps(err_event), file=sys.stderr)
