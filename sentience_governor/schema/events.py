"""Core event schema — enums, payload models, and governance event envelope."""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field, model_serializer


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    AGENT_REGISTERED = "AGENT_REGISTERED"
    INTENT_DECLARED = "INTENT_DECLARED"
    SCOPE_ASSERTED = "SCOPE_ASSERTED"
    CONTEXT_SNAPSHOT = "CONTEXT_SNAPSHOT"
    MEMORY_WRITE_ATTEMPT = "MEMORY_WRITE_ATTEMPT"
    GOVERNANCE_ERROR = "GOVERNANCE_ERROR"


class DeploymentMode(str, Enum):
    vendor_managed = "vendor_managed"
    enterprise_managed = "enterprise_managed"


class PrimitiveType(str, Enum):
    REGISTRATION = "REGISTRATION"
    INTENT = "INTENT"
    SCOPE = "SCOPE"
    CONTEXT = "CONTEXT"
    MEMORY = "MEMORY"
    SYSTEM = "SYSTEM"


class IntentSource(str, Enum):
    """Where the session's stated objective came from.

    ``explicit``  - supplied by the integrator at wrapper construction time
                    (e.g. ``wrap_mcp_client(stated_objective="...")``).
                    Static, code-resident, integrator-vouched.

    ``inferred``  - extracted from invocation context at runtime
                    (e.g. the LangChain callback handler reading chain
                    inputs). Runtime-present; the extraction mechanism
                    is reliable but the content's meaning is opaque
                    (could be a user request, a machine-generated payload,
                    etc.).

    ``none``      - no objective signal of any kind was available.

    The distinction between ``explicit`` and ``inferred`` is load-bearing
    for the audit trail: the wrapper's governance value depends on the
    trace being honest about whether an objective came from the integrator
    ahead of time or from runtime invocation context.
    """
    explicit = "explicit"
    inferred = "inferred"
    none = "none"


class IntentConfidence(str, Enum):
    """Epistemic trust in the content of the stated objective.

    Confidence reflects trust in what the objective *means*, not the
    reliability of how the objective was obtained. A string extracted
    reliably from invocation inputs is still low-confidence content
    because we do not know what it means or whether it is authorized.

    ``explicit``       - integrator-declared at wrapper construction
                         time; high trust in both content and mechanism.
    ``inferred_high``  - extracted from context with high semantic trust
                         (reserved for future use; the current runtime
                         does not produce this value).
    ``inferred_low``   - extracted reliably but content meaning is opaque
                         (the typical case for LangChain input extraction
                         and similar runtime-context sources).
    ``unknown``        - no objective signal was available.
    """
    explicit = "explicit"
    inferred_high = "inferred_high"
    inferred_low = "inferred_low"
    unknown = "unknown"


class OperationType(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    DELETE = "DELETE"
    EXECUTE = "EXECUTE"


class ClassificationSource(str, Enum):
    explicit = "explicit"
    vendor = "vendor"
    unclassified = "unclassified"


class WriteType(str, Enum):
    explicit_persist = "explicit_persist"
    write_to_persistence_target = "write_to_persistence_target"


class DetectionMechanism(str, Enum):
    tool_metadata = "tool_metadata"
    config_registry = "config_registry"
    default_fallback_list = "default_fallback_list"


class ErrorType(str, Enum):
    INTERCEPT_FAILURE = "INTERCEPT_FAILURE"
    SINK_UNAVAILABLE = "SINK_UNAVAILABLE"
    SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
    TIMEOUT = "TIMEOUT"


class Severity(str, Enum):
    warning = "warning"
    degraded = "degraded"
    critical = "critical"


class InterceptStage(str, Enum):
    REGISTRATION = "REGISTRATION"
    INTENT = "INTENT"
    SCOPE = "SCOPE"
    CONTEXT = "CONTEXT"
    MEMORY = "MEMORY"


class AdvisoryFlag(str, Enum):
    AGENT_UNREGISTERED = "AGENT_UNREGISTERED"
    INTENT_MISSING = "INTENT_MISSING"
    SCOPE_OPERATION_UNEXPECTED = "SCOPE_OPERATION_UNEXPECTED"
    SCOPE_INTENT_MISMATCH = "SCOPE_INTENT_MISMATCH"
    CONTEXT_UNCLASSIFIED = "CONTEXT_UNCLASSIFIED"
    SENSITIVITY_ESCALATION = "SENSITIVITY_ESCALATION"
    MEMORY_WRITE_UNCLASSIFIED = "MEMORY_WRITE_UNCLASSIFIED"
    MEMORY_WRITE_CANDIDATE = "MEMORY_WRITE_CANDIDATE"
    # v0.2.5 governance-profile-driven flags. Fire only when an
    # operator-authored profile is active for the session AND the
    # profile-defined signal/pattern matches. Backward-compat: never
    # fire for sessions running without a profile, so v0.2.4 traces
    # are byte-identical under v0.2.5.
    TASK_BOUNDARY_CROSSED = "TASK_BOUNDARY_CROSSED"
    HIGH_CONSEQUENCE_DETECTED = "HIGH_CONSEQUENCE_DETECTED"


class PolicyViolation(str, Enum):
    POL_001 = "POL-001"
    POL_002 = "POL-002"
    POL_003 = "POL-003"
    POL_004 = "POL-004"
    POL_005 = "POL-005"


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------

class AgentRegisteredPayload(BaseModel):
    agent_id: str
    agent_version: Optional[str] = None
    vendor_id: Optional[str] = None
    deployment_mode: DeploymentMode
    declared_capabilities: List[str] = Field(default_factory=list)
    owner_claim: Optional[str] = None
    policy_context: Optional[str] = None

    # v0.2.5 — profile metadata. Both fields are populated by
    # EventBuilder.build_agent_registered when the session is
    # governed by an operator-authored profile. Both serialize with
    # None-omission (no profile loaded → fields absent from JSON),
    # so v0.2.4-shaped traces are byte-identical under v0.2.5.
    profile_loaded: Optional[bool] = None
    profile_schema_version: Optional[int] = None

    @model_serializer(mode="wrap")
    def serialize_with_profile_omission(self, default_handler):
        raw = default_handler(self)
        for field in ("profile_loaded", "profile_schema_version"):
            if raw.get(field) is None:
                raw.pop(field, None)
        return raw


class IntentDeclaredPayload(BaseModel):
    stated_objective: Optional[str] = None
    intent_source: IntentSource
    intent_confidence: IntentConfidence
    authorization_claim: Optional[str] = None
    session_scope_hint: List[str] = Field(default_factory=list)


class ScopeAssertedPayload(BaseModel):
    tool_id: str
    asserted_permissions: List[str] = Field(default_factory=list)
    target_system: str
    operation_type: OperationType

    # v0.2.6.1 — Claude Code join key. The `tool_use_id` (`toolu_…`) the
    # live hook saw for this tool call, so a POL-001 violation here can be
    # joined to its model turn (requestId) by the SessionEnd token batch.
    # Optional + None-omitted: non-Claude-Code paths (MCP/LangChain) leave
    # it None and the field never appears, so existing traces are unchanged.
    tool_use_id: Optional[str] = None

    @model_serializer(mode="wrap")
    def _omit_none_tool_use_id(self, default_handler):
        raw = default_handler(self)
        if raw.get("tool_use_id") is None:
            raw.pop("tool_use_id", None)
        return raw


class ContextSnapshotPayload(BaseModel):
    data_classifications: List[str] = Field(default_factory=list)
    classification_source: ClassificationSource
    provenance: List[str] = Field(default_factory=list)
    retention_flags: List[str] = Field(default_factory=list)
    context_size_tokens: int

    # Approach A — LLM token accounting (v0.2.3 Track 2). All optional.
    # On serialisation, fields whose value is None are OMITTED entirely
    # from the JSON payload (so non-adopters see no schema bloat); fields
    # whose value is 0 are PRESERVED (zero is a real measurement, not
    # absence). See ContextSnapshotPayload.serialize_with_token_omission
    # below for the omission mechanism.
    #
    # llm_turn_id is the dedupe identity: when one LLM turn produces
    # multiple events, every event from that turn carries the same
    # llm_turn_id so downstream consumers can dedupe before summing
    # token counts. Events with llm_turn_id=None have no
    # framework-provided dedupe identity.
    llm_prompt_tokens: Optional[int] = None
    llm_completion_tokens: Optional[int] = None
    llm_cached_read_tokens: Optional[int] = None
    llm_cached_write_tokens: Optional[int] = None
    llm_reasoning_tokens: Optional[int] = None
    model_identifier: Optional[str] = None
    provider: Optional[str] = None
    llm_turn_id: Optional[str] = None

    # v0.2.6.1 — Claude Code join keys.
    #  * tool_use_id (singular): the live pre/post-call snapshot's own tool
    #    call, so a POL-003/POL-005 violation here joins to its turn.
    #  * tool_use_ids (list): set ONLY on the SessionEnd token-bearing
    #    snapshot for a turn — the tool_use_ids that turn (requestId) issued,
    #    the reverse-map the analyzer uses to attribute a violation's burn.
    # Both Optional + None-omitted, like the token fields above.
    tool_use_id: Optional[str] = None
    tool_use_ids: Optional[List[str]] = None

    # Centralised serialization layer (per plan §1.3): the rule "None
    # omitted, 0 preserved" lives HERE, not independently inside each
    # integration path. Existing required / list fields are unchanged.
    @model_serializer(mode="wrap")
    def serialize_with_token_omission(self, default_handler):
        raw = default_handler(self)
        # Only the new (v0.2.3 Track 2 + v0.2.6.1 join) optional fields are
        # subject to None-omission. Existing fields keep their current shape.
        token_optional_fields = (
            "llm_prompt_tokens",
            "llm_completion_tokens",
            "llm_cached_read_tokens",
            "llm_cached_write_tokens",
            "llm_reasoning_tokens",
            "model_identifier",
            "provider",
            "llm_turn_id",
            "tool_use_id",
            "tool_use_ids",
        )
        for field in token_optional_fields:
            if raw.get(field) is None:
                raw.pop(field, None)
        return raw


class MemoryWriteAttemptPayload(BaseModel):
    write_type: WriteType
    detection_mechanism: Optional[DetectionMechanism] = None
    target_store: str
    write_classification: str
    write_size_tokens: int
    retention_requested: Optional[str] = None

    # v0.2.6.1 — Claude Code join key, so a POL-004 violation on a memory
    # write (Claude Code's unclassified Edit/Write) joins to its model turn.
    # Optional + None-omitted (non-Claude-Code paths leave it None).
    tool_use_id: Optional[str] = None

    @model_serializer(mode="wrap")
    def _omit_none_tool_use_id(self, default_handler):
        raw = default_handler(self)
        if raw.get("tool_use_id") is None:
            raw.pop("tool_use_id", None)
        return raw


class GovernanceErrorPayload(BaseModel):
    error_type: ErrorType
    severity: Severity
    intercept_stage: Optional[InterceptStage] = None
    failure_reason: str
    agent_continued: bool = True


AnyPayload = Union[
    AgentRegisteredPayload,
    IntentDeclaredPayload,
    ScopeAssertedPayload,
    ContextSnapshotPayload,
    MemoryWriteAttemptPayload,
    GovernanceErrorPayload,
]


# ---------------------------------------------------------------------------
# Core event envelope
# ---------------------------------------------------------------------------

class GovernanceEvent(BaseModel):
    event_id: str
    event_type: EventType
    session_id: str
    event_sequence_number: int
    previous_event_id: Optional[str] = None
    agent_id: str
    deployment_mode: DeploymentMode
    timestamp_utc: str
    primitive: PrimitiveType
    payload: Any  # validated by EventBuilder before construction
    advisory_flags: List[str] = Field(default_factory=list)
    policy_violations: List[str] = Field(default_factory=list)
    simulated_consequence: Optional[str] = None
    pass_through: bool = True

    # v0.2.5 — envelope-level profile fingerprint. 12-char hex prefix
    # of the SHA256 of the profile's canonical JSON form. Populated on
    # EVERY event when the session was started under a profile.
    # None-omitted on serialization so v0.2.4-shaped traces are
    # byte-identical under v0.2.5.
    profile_fingerprint: Optional[str] = None

    @model_serializer(mode="wrap")
    def serialize_with_envelope_omission(self, default_handler):
        raw = default_handler(self)
        if raw.get("profile_fingerprint") is None:
            raw.pop("profile_fingerprint", None)
        # Ensure payload is a plain dict — keep the existing pattern
        # from to_dict() so model_dump() output matches.
        if hasattr(self.payload, "model_dump"):
            raw["payload"] = self.payload.model_dump()
        return raw

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dict matching the golden trace schema."""
        # Now delegates to the serializer above so to_dict() and
        # model_dump() agree on None-omission behavior.
        return self.model_dump()
