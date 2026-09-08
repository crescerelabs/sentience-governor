"""CP4 — explicit-first classification, honest absence, D2 fail-open.

The acceptance criterion is a negative: **no code path derives an
operation or a target system from a tool name.** Several tests exist only
to keep it that way.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any, List, Optional

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool

from sentience_governor.schema.events import ClassificationSource, OperationType

from pydantic_ai_governor.evidence import Operation

from pydantic_ai_governor import SentienceGovernor, evidence
from pydantic_ai_governor.evidence import Classification

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def governance_errors(capsys) -> List[dict]:
    """Read from stdout: core routes GOVERNANCE_ERROR there regardless of
    the configured sink (`sink/writer.py:124-127`)."""
    events = []
    for line in capsys.readouterr().out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event_type") == "GOVERNANCE_ERROR":
            events.append(event)
    return events


def trace_events(home: Path, session_id: str) -> List[dict]:
    path = home / ".sentience" / "traces" / "pydantic-ai" / f"{session_id}.jsonl"
    return ([json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            if path.exists() else [])


def one_call_model(tool_name: str) -> FunctionModel:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name, {"x": "a"})])
        return ModelResponse(parts=[TextPart("done")])
    return FunctionModel(fn)


def tool_named(name: str, metadata: Optional[dict] = None) -> Tool:
    def _impl(x: str) -> str:
        return f"result:{x}"
    _impl.__name__ = name
    _impl.__doc__ = "A tool."
    return Tool(_impl, metadata=metadata) if metadata else Tool(_impl)


class _Capture(SentienceGovernor):
    """Records the classification each call resolved to.

    As of CP5 the capability emits the scope assertion and the context
    snapshot itself, so this no longer builds duplicate events: it observes
    the resolved classification and reads the policy outcome back off the
    real trace. The same dissolution CP3 performed on CP2's probe.
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.seen: List[evidence.Classification] = []
        self.home: Optional[Path] = None

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        result = await super().wrap_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args, handler=handler
        )
        # Resolved from the same input the capability resolves from,
        # rather than read back off the instance. CP7 forbids per-call
        # state on `self`, and a probe reaching for it would keep that
        # state alive purely to be observed.
        seen, _ = evidence.resolve(getattr(tool_def, "metadata", None))
        self.seen.append(seen)
        return result

    def _written(self) -> List[dict]:
        base = self.home / ".sentience" / "traces" / "pydantic-ai"
        out: List[dict] = []
        for path in sorted(base.glob("*.jsonl")):
            out += [json.loads(l) for l in path.read_text().splitlines()
                    if l.strip()]
        return out

    def _first(self, event_type: str) -> dict:
        return next(e for e in self._written() if e["event_type"] == event_type)

    def _first_tool_snapshot(self) -> dict:
        """The tool-result snapshot, not a CP6 model-turn token snapshot.

        Both carry the CONTEXT_SNAPSHOT type, and the token one is emitted
        first (the model turn precedes the tool call it requests), so a
        plain "first CONTEXT_SNAPSHOT" would read classification evidence
        off an event that makes no classification claim.
        """
        return next(e for e in self._written()
                    if e["event_type"] == "CONTEXT_SNAPSHOT"
                    and e["payload"].get("llm_turn_id") is None)

    @property
    def scope_violations(self) -> List[str]:
        """POL-001 and friends, from the REAL scope assertion."""
        return list(self._first("SCOPE_ASSERTED").get("policy_violations") or [])

    @property
    def snapshot_violations(self) -> List[str]:
        """POL-003 and friends, from the REAL context snapshot."""
        return list(self._first_tool_snapshot().get("policy_violations") or [])

    @property
    def snapshot_flags(self) -> List[str]:
        return list(self._first_tool_snapshot().get("advisory_flags") or [])


async def drive(gov: SentienceGovernor, tool: Tool, home: Path = None,
                **kw: Any):
    if isinstance(gov, _Capture):
        gov.home = home if home is not None else Path(os.environ["HOME"])
    agent = Agent(one_call_model(tool.name), tools=[tool], capabilities=[gov])
    return await agent.run("go", **kw)


# ---------------------------------------------------------------------------
# Explicit classification
# ---------------------------------------------------------------------------

CRM_READ = {"sentience_governor": {
    "operation": "READ", "target_system": "crm", "classification": ["internal"]}}


async def test_explicit_metadata_is_recorded(isolated_home):
    gov = _Capture(objective="Fix the timeout", scope=["crm"])
    await drive(gov, tool_named("crm_fetch", CRM_READ))
    [c] = gov.seen
    assert c.operation == Operation.READ
    assert c.operation_declared
    assert c.target_system == "crm"
    assert c.data_classifications == ("internal",)
    assert c.source == ClassificationSource.explicit
    assert c.core_operation() == (OperationType.READ, ["read"])


async def test_explicit_classification_drives_policy_in_scope(isolated_home):
    """Declared scope covers the declared target: no POL-001."""
    gov = _Capture(objective="Fix the timeout", scope=["crm"])
    await drive(gov, tool_named("crm_fetch", CRM_READ))
    assert gov.scope_violations == []


async def test_explicit_classification_drives_policy_out_of_scope(isolated_home):
    """Same tool, a scope that excludes it: POL-001 fires."""
    gov = _Capture(objective="Fix the timeout", scope=["billing"])
    await drive(gov, tool_named("crm_fetch", CRM_READ))
    assert "POL-001" in gov.scope_violations


async def test_pol_003_suppressed_when_classification_is_explicit(isolated_home):
    gov = _Capture(objective="o", scope=["crm"])
    await drive(gov, tool_named("crm_fetch", CRM_READ))
    assert gov.snapshot_flags == []
    assert gov.snapshot_violations == []


# ---------------------------------------------------------------------------
# Honest absence — the acceptance criterion
# ---------------------------------------------------------------------------

async def test_db_delete_record_does_not_become_delete_on_db(isolated_home):
    """The rule that recurs through this whole integration. A tool's name
    is not evidence about what it does."""
    gov = _Capture(objective="o", scope=["crm"])
    await drive(gov, tool_named("db_delete_record"))

    [c] = gov.seen
    assert c.operation is Operation.UNKNOWN
    assert not c.operation_declared
    assert c.target_system is None
    assert c.target_for("db_delete_record") == "db_delete_record"
    assert c.source == ClassificationSource.unclassified


async def test_absent_classification_keeps_pol_003(isolated_home):
    gov = _Capture(objective="o", scope=["crm"])
    await drive(gov, tool_named("db_delete_record"))
    assert "CONTEXT_UNCLASSIFIED" in gov.snapshot_flags
    assert "POL-003" in gov.snapshot_violations


async def test_absent_classification_is_not_an_error(isolated_home, capsys):
    """Missing is not malformed. No warning, no GOVERNANCE_ERROR."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await drive(_Capture(objective="o", scope=["crm"]),
                    tool_named("db_delete_record"))
    assert [w for w in caught if "classification" in str(w.message)] == []
    assert governance_errors(capsys) == []


def test_no_inference_machinery_exists_in_the_package():
    """A sweep, not a promise. Nothing may parse a tool name into an
    operation or a bucket, and the MCP wrapper's keyword classifier must
    not be reused."""
    src = Path(evidence.__file__).parent
    banned = ("_infer_operation_type", "infer_operation", "KEYWORD",
              "_PERSISTENCE_KEYWORDS", "startswith(\"delete\")", "'delete' in")
    for path in src.rglob("*.py"):
        text = path.read_text()
        for token in banned:
            assert token not in text, f"{path.name} contains {token!r}"


def test_operation_matching_is_exact_not_fuzzy():
    """`"read"` is not `"READ"`. Accepting near-misses would be inference
    by another name, and the developer would never learn that what they
    wrote was not what was recorded."""
    assert evidence.validate({"operation": "read"}) is not None
    assert evidence.validate({"operation": "Read"}) is not None
    assert evidence.validate({"operation": "READ"}) is None


# ---------------------------------------------------------------------------
# D2 — visible fail-open
# ---------------------------------------------------------------------------

MALFORMED = [
    pytest.param("not-a-mapping", id="block-is-a-string"),
    pytest.param({"operation": "REDACT"}, id="unknown-operation"),
    pytest.param({"operation": "read"}, id="operation-wrong-case"),
    pytest.param({"target_system": 7}, id="target-not-a-string"),
    pytest.param({"target_system": " "}, id="target-blank"),
    pytest.param({"classification": "internal"}, id="classification-a-string"),
    pytest.param({"classification": ["internal", 3]}, id="classification-item"),
]


@pytest.mark.parametrize("block", MALFORMED)
async def test_malformed_is_visible_and_never_blocks(isolated_home, block, capsys):
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning, match="ignored the classification"):
        result = await drive(gov, tool_named("crm_fetch",
                                             {"sentience_governor": block}))

    # The tool executed and the run completed.
    assert result.output

    errors = governance_errors(capsys)
    assert len(errors) == 1
    assert errors[0]["payload"]["error_type"] == "SCHEMA_VIOLATION"
    assert errors[0]["payload"]["severity"] == "warning"
    assert errors[0]["payload"]["agent_continued"] is True
    # The tool is named so the developer knows which one to fix.
    assert "crm_fetch" in errors[0]["payload"]["failure_reason"]


async def test_malformed_records_the_call_as_unclassified(isolated_home):
    """Rejected is not the same as explicit, and the evidence says so."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("crm_fetch", {"sentience_governor": {
            "operation": "REDACT"}}))
    [c] = gov.seen
    assert c.source == ClassificationSource.unclassified
    assert "CONTEXT_UNCLASSIFIED" in gov.snapshot_flags
    assert "POL-003" in gov.snapshot_violations


async def test_rejected_classification_does_not_license_guessing(isolated_home):
    """A rejected block leaves the call unclassified. The tool name is
    still not mined for a substitute."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("db_delete_record",
                                    {"sentience_governor": {"operation": "NOPE"}}))
    [c] = gov.seen
    assert c.operation is Operation.UNKNOWN
    assert c.target_for("db_delete_record") == "db_delete_record"


async def test_malformed_block_is_rejected_as_a_unit(isolated_home):
    """A good operation beside a broken classification yields nothing. A
    partial classification recorded as explicit would claim more than the
    developer supplied."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("crm_fetch", {"sentience_governor": {
            "operation": "READ", "target_system": "crm",
            "classification": {"internal": True}}}))
    [c] = gov.seen
    assert c.operation is Operation.UNKNOWN
    assert c.target_system is None
    assert c.data_classifications == ()
    assert c.source == ClassificationSource.unclassified


async def test_no_metadata_value_reaches_the_warning_or_the_error(
    isolated_home, capsys
):
    secret = "sk-live-TOOL-METADATA-9c1d"
    gov = _Capture(objective="o", scope=["crm"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await drive(gov, tool_named("crm_fetch", {"sentience_governor": {
            "target_system": secret, "operation": "NOPE"}}))

    assert caught
    for warning in caught:
        assert secret not in str(warning.message)
    assert secret not in json.dumps(governance_errors(capsys))


async def test_unknown_tool_key_names_the_key_not_its_value(
    isolated_home, capsys
):
    secret = "sk-live-UNKNOWN-TOOL-KEY-2f8a"
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("crm_fetch", {"sentience_governor": {
            "mystery": secret}}))
    blob = json.dumps(governance_errors(capsys))
    assert "mystery" in blob
    assert secret not in blob


# ---------------------------------------------------------------------------
# Rev 6 — unknown keys are reported, not destructive
# ---------------------------------------------------------------------------

UNKNOWN_BESIDE_VALID = {"sentience_governor": {
    "operation": "READ", "target_system": "crm",
    "classification": ["internal"], "foo": "bar"}}


async def test_unknown_key_does_not_discard_valid_fields(isolated_home):
    """The case that drove Rev 6. A stray key must not turn a truthful
    (READ, crm, internal) into unclassified."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("crm_fetch", UNKNOWN_BESIDE_VALID))

    [c] = gov.seen
    assert c.operation == OperationType.READ
    assert c.target_system == "crm"
    assert c.data_classifications == ("internal",)
    assert c.source == ClassificationSource.explicit


async def test_unknown_key_is_still_visibly_reported(isolated_home, capsys):
    """Preserved evidence is not the same as silence."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning, match="ignored the classification"):
        await drive(gov, tool_named("crm_fetch", UNKNOWN_BESIDE_VALID))
    errors = governance_errors(capsys)
    assert len(errors) == 1
    assert "'foo'" in errors[0]["payload"]["failure_reason"]


async def test_partial_acceptance_states_what_was_retained(
    isolated_home, capsys
):
    """Without this a developer cannot tell whether their classification
    survived, and partial acceptance would be quieter in practice than
    rejection despite emitting the same error."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("crm_fetch", {"sentience_governor": {
            "operation": "READ", "targt_system": "crm"}}))

    reason = governance_errors(capsys)[0]["payload"]["failure_reason"]
    assert "'targt_system'" in reason          # what was not understood
    assert "retained 'operation'" in reason     # what survived
    assert "fell back" in reason                # what did not
    assert "not mapped" in reason               # and nothing was guessed


async def test_unknown_key_is_never_mapped_to_a_recognised_field(isolated_home):
    """`targt_system` is not read as `target_system`. The recognised field
    is absent, so honest-absence applies and the tool name is used."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("crm_fetch", {"sentience_governor": {
            "operation": "READ", "targt_system": "crm"}}))
    [c] = gov.seen
    assert c.target_system is None
    assert c.target_for("crm_fetch") == "crm_fetch"


async def test_invalid_recognised_field_still_rejects_atomically(isolated_home):
    """The asymmetry inside D2 itself: an unknown key is tolerated, an
    invalid recognised value is not."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("crm_fetch", {"sentience_governor": {
            "operation": "read", "classification": ["internal"], "foo": 1}}))
    [c] = gov.seen
    assert c.operation is Operation.UNKNOWN
    assert c.data_classifications == ()
    assert c.source == ClassificationSource.unclassified


# ---------------------------------------------------------------------------
# Rev 6 — ClassificationSource follows the data classifications
# ---------------------------------------------------------------------------

async def test_source_is_explicit_only_when_classification_was_supplied(
    isolated_home,
):
    """Corrects a CP4 defect. A block declaring an operation but no
    classification said nothing about the data, so stamping `explicit`
    suppressed POL-003 on a call that was never classified."""
    gov = _Capture(objective="o", scope=["crm"])
    await drive(gov, tool_named("crm_fetch", {"sentience_governor": {
        "operation": "READ", "target_system": "crm"}}))

    [c] = gov.seen
    assert c.operation == Operation.READ          # the operation is kept
    assert c.source == ClassificationSource.unclassified
    assert "CONTEXT_UNCLASSIFIED" in gov.snapshot_flags
    assert "POL-003" in gov.snapshot_violations


def test_source_tracks_the_classification_field_directly():
    supplied, _ = evidence.resolve({"sentience_governor": {
        "classification": ["internal"]}})
    assert supplied.source == ClassificationSource.explicit

    absent, _ = evidence.resolve({"sentience_governor": {"operation": "READ"}})
    assert absent.source == ClassificationSource.unclassified

    empty, _ = evidence.resolve({"sentience_governor": {"classification": []}})
    assert empty.source == ClassificationSource.explicit


async def test_unrelated_tool_metadata_is_ignored(isolated_home, capsys):
    """Another library's tool metadata is not our contract to police."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gov = _Capture(objective="o", scope=["crm"])
        await drive(gov, tool_named("crm_fetch", {"other_tool": {"x": 1}}))
    assert [w for w in caught if "classification" in str(w.message)] == []
    assert governance_errors(capsys) == []
    assert gov.seen[0].source == ClassificationSource.unclassified


# ---------------------------------------------------------------------------
# The resolver, directly
# ---------------------------------------------------------------------------

def test_validate_accepts_a_well_formed_block():
    assert evidence.validate({"operation": "WRITE", "target_system": "crm",
                              "classification": ["internal"]}) is None
    assert evidence.validate({}) is None


def test_validate_does_not_reject_on_unknown_keys():
    """Rev 6: unknown keys are reported by `unknown_keys`, not by rejecting."""
    assert evidence.validate({"operation": "READ", "foo": "bar"}) is None
    assert evidence.unknown_keys({"operation": "READ", "foo": "bar"}) == ("foo",)
    assert evidence.unknown_keys({"operation": "READ"}) == ()


def test_multiple_unknown_keys_are_all_named():
    assert evidence.unknown_keys({"beta": 2, "alpha": 1}) == ("alpha", "beta")


def test_resolve_treats_a_missing_namespace_as_absent():
    classification, rejection = evidence.resolve({"other": {"x": 1}})
    assert rejection is None
    assert classification.source == ClassificationSource.unclassified


def test_every_operation_type_is_accepted():
    for member in OperationType:
        assert evidence.validate({"operation": member.value}) is None


# ---------------------------------------------------------------------------
# Rev 7 — UNKNOWN is internal; the boundary mapping is compatibility only
# ---------------------------------------------------------------------------

def test_unknown_is_the_default_and_is_never_a_core_type():
    """The integration carries a semantic core cannot express."""
    assert Classification().operation is Operation.UNKNOWN
    assert "UNKNOWN" not in {m.value for m in OperationType}


def test_unknown_maps_to_read_with_no_permissions():
    """Compatibility only. READ is chosen because it is the one
    non-mutating member: anything else would manufacture POL-001 in an
    undeclared session on the strength of a fallback."""
    assert evidence.to_core_operation(Operation.UNKNOWN) == (
        OperationType.READ, [])


def test_declared_read_is_distinguishable_from_the_fallback():
    """Same operation_type, different permissions. That difference is the
    only marker, and it is a marker rather than a mechanism."""
    declared = evidence.to_core_operation(Operation.READ)
    fallback = evidence.to_core_operation(Operation.UNKNOWN)
    assert declared == (OperationType.READ, ["read"])
    assert fallback == (OperationType.READ, [])
    assert declared[0] == fallback[0]
    assert declared[1] != fallback[1]


def test_every_declared_operation_round_trips():
    for member in (Operation.READ, Operation.WRITE, Operation.DELETE,
                   Operation.EXECUTE):
        op_type, perms = evidence.to_core_operation(member)
        assert op_type.value == member.value
        assert perms == [member.value.lower()]


def test_nothing_interprets_empty_permissions_as_unknown():
    """Rev 7 forbids a second classification channel on a field core
    carries but never evaluates."""
    src = Path(evidence.__file__).parent
    for path in src.rglob("*.py"):
        text = path.read_text()
        assert "asserted_permissions ==" not in text
        assert "permissions == []" not in text


def test_context_estimate_matches_core_semantics():
    """Same estimator core ships at wrapper/mcp.py:406-412, so the field
    carries the meaning the product already gives it."""
    import json as _json
    for value in ("x" * 40, {"a": 1}, ["a", "b"], 5, None):
        expected = max(1, len(_json.dumps(value)) // 4)
        assert evidence.estimate_context_tokens(value) == expected


def test_context_estimate_is_never_a_character_count():
    """The defect this replaced: a long result must not report its length."""
    value = "x" * 400
    assert evidence.estimate_context_tokens(value) != len(value)
    assert evidence.estimate_context_tokens(value) < len(value)


def test_context_estimate_never_raises_and_never_returns_zero():
    class Unserialisable:
        def __repr__(self): return "<obj>"
    assert evidence.estimate_context_tokens(Unserialisable()) == 1
    assert evidence.estimate_context_tokens("") >= 1
