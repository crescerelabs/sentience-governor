"""CP4 — explicit-first classification, honest absence, D2 fail-open.

The acceptance criterion is a negative: **no code path derives an
operation or a target system from a tool name.** Several tests exist only
to keep it that way.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, List, Optional

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import Tool

from sentience_governor.schema.events import ClassificationSource, OperationType

from pydantic_ai_governor import SentienceGovernor, evidence

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

    CP4 resolves classification; emitting a scope assertion from it is the
    next checkpoint's surface, so the assertion below is driven from here.
    The same pattern CP2 used for the declaration, and CP5 replaces it.
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.seen: List[evidence.Classification] = []
        self.violations: List[List[str]] = []
        self.flags: List[List[str]] = []

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        result = await super().wrap_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args, handler=handler
        )
        c = self._classification
        self.seen.append(c)

        scope = self._builder.build_scope_asserted(
            tool_id=call.tool_name,
            asserted_permissions=c.asserted_permissions(),
            target_system=c.target_for(call.tool_name),
            operation_type=c.operation or OperationType.EXECUTE,
        )
        self.violations.append(list(scope.policy_violations or []))

        snapshot = self._builder.build_context_snapshot(
            data_classifications=list(c.data_classifications),
            classification_source=c.source,
            provenance=[c.target_for(call.tool_name)],
            retention_flags=[],
            context_size_tokens=len(str(result)),
        )
        self.flags.append(list(snapshot.advisory_flags or []))
        self.violations.append(list(snapshot.policy_violations or []))
        return result


async def drive(gov: SentienceGovernor, tool: Tool, **kw: Any):
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
    assert c.operation == OperationType.READ
    assert c.target_system == "crm"
    assert c.data_classifications == ("internal",)
    assert c.source == ClassificationSource.explicit
    assert c.asserted_permissions() == ["read"]


async def test_explicit_classification_drives_policy_in_scope(isolated_home):
    """Declared scope covers the declared target: no POL-001."""
    gov = _Capture(objective="Fix the timeout", scope=["crm"])
    await drive(gov, tool_named("crm_fetch", CRM_READ))
    assert gov.violations[0] == []


async def test_explicit_classification_drives_policy_out_of_scope(isolated_home):
    """Same tool, a scope that excludes it: POL-001 fires."""
    gov = _Capture(objective="Fix the timeout", scope=["billing"])
    await drive(gov, tool_named("crm_fetch", CRM_READ))
    assert "POL-001" in gov.violations[0]


async def test_pol_003_suppressed_when_classification_is_explicit(isolated_home):
    gov = _Capture(objective="o", scope=["crm"])
    await drive(gov, tool_named("crm_fetch", CRM_READ))
    assert gov.flags[0] == []
    assert gov.violations[1] == []


# ---------------------------------------------------------------------------
# Honest absence — the acceptance criterion
# ---------------------------------------------------------------------------

async def test_db_delete_record_does_not_become_delete_on_db(isolated_home):
    """The rule that recurs through this whole integration. A tool's name
    is not evidence about what it does."""
    gov = _Capture(objective="o", scope=["crm"])
    await drive(gov, tool_named("db_delete_record"))

    [c] = gov.seen
    assert c.operation is None
    assert c.target_system is None
    assert c.asserted_permissions() == []
    assert c.target_for("db_delete_record") == "db_delete_record"
    assert c.source == ClassificationSource.unclassified


async def test_absent_classification_keeps_pol_003(isolated_home):
    gov = _Capture(objective="o", scope=["crm"])
    await drive(gov, tool_named("db_delete_record"))
    assert "CONTEXT_UNCLASSIFIED" in gov.flags[0]
    assert "POL-003" in gov.violations[1]


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
    assert "CONTEXT_UNCLASSIFIED" in gov.flags[0]
    assert "POL-003" in gov.violations[1]


async def test_rejected_classification_does_not_license_guessing(isolated_home):
    """A rejected block leaves the call unclassified. The tool name is
    still not mined for a substitute."""
    gov = _Capture(objective="o", scope=["crm"])
    with pytest.warns(UserWarning):
        await drive(gov, tool_named("db_delete_record",
                                    {"sentience_governor": {"operation": "NOPE"}}))
    [c] = gov.seen
    assert c.operation is None
    assert c.asserted_permissions() == []
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
    assert c.operation is None
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
    assert c.operation is None
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
    assert c.operation == OperationType.READ       # the operation is kept
    assert c.source == ClassificationSource.unclassified
    assert "CONTEXT_UNCLASSIFIED" in gov.flags[0]
    assert "POL-003" in gov.violations[1]


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
