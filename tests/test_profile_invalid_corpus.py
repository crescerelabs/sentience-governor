"""Regression corpus for issue #19: parseable-but-invalid governance profiles.

The fixtures under ``tests/fixtures/profiles/invalid/`` and their manifest
``corpus.json`` cover every malformed-but-parseable profile shape found while
grounding the fix, including shapes that load, pass ``validate()`` and then
raise inside runtime evaluation on 0.3.2.

Written before the fix. Against the 0.3.2 implementation many of these tests
FAIL by design; the failures specify the corrected behavior:

* Category A (error-level): the profile loads, ``validate()`` names the field,
  a matched binding resolves ``degraded``, ``session_start`` refuses to activate
  it, and a governed run forced onto it completes without raising and without
  policy manufactured from the malformed value.
* Category B (warning-only): loads, no errors, at least one warning, binds,
  runs. Acceptable under current semantics.
* Category C (direct-runtime tolerance): Category A fixtures forced past
  ``session_start`` through a test seam, exercising the builder's defensive
  path on its own.
* Category F (construction-rejected): ``GovernanceProfile`` cannot be built
  at all; the exception type is part of the contract.

Test seam: Category C assigns the profile onto the session entry directly,
bypassing ``session_start``. This is deliberate and confined to this module.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
import yaml

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile.loader import GovernanceProfile
from sentience_governor.profile.resolver import (
    SOURCE_BOUND,
    SOURCE_DEGRADED,
    SOURCE_NONE,
    resolve_profile,
)
from sentience_governor.schema.events import AdvisoryFlag, OperationType, PolicyViolation
from sentience_governor.session_manager.manager import SessionManager

CORPUS_DIR = Path(__file__).parent / "fixtures" / "profiles" / "invalid"
MANIFEST = json.loads((CORPUS_DIR / "corpus.json").read_text(encoding="utf-8"))
FIXTURES = MANIFEST["fixtures"]
BY_CAT = {c: [f for f in FIXTURES if f["category"] == c] for c in ("A", "B", "F")}
IDS = lambda cat: [f["file"] for f in BY_CAT[cat]]  # noqa: E731

VALID_DEFAULT = (
    "schema_version: 1\n"
    "session_intent:\n  demand_at: session_start\n"
    "task_boundary:\n  signals: []\n"
    "high_consequence:\n  tools: ['deploy:.*']\n"
)


def _path(fx: Dict[str, Any]) -> Path:
    return CORPUS_DIR / fx["file"]


def _governed_run(sm: SessionManager, cache: InProcessCache, sid: str, **builder_kw) -> Tuple[Any, List[Any]]:
    """Registration plus three scope assertions (the issue #19 reproduction shape,
    across two directories so task-boundary parameters are exercised)."""
    b = EventBuilder(sm, cache, "agent", sid, **builder_kw)
    reg = b.build_agent_registered(agent_version=None, vendor_id="corpus", declared_capabilities=[], owner_claim=None)
    shape = MANIFEST["run_shape"]
    scopes = [
        b.build_scope_asserted(tool_id="fs_read", asserted_permissions=[], target_system=shape["fs_read"],
                               operation_type=OperationType.READ, tool_use_id="t1"),
        b.build_scope_asserted(tool_id="fs_write", asserted_permissions=[], target_system=shape["fs_write"],
                               operation_type=OperationType.WRITE, tool_use_id="t2"),
        b.build_scope_asserted(tool_id="Bash", asserted_permissions=[], target_system=shape["bash"],
                               operation_type=OperationType.EXECUTE, tool_use_id="t3"),
    ]
    return reg, scopes


def _force_profile(sm: SessionManager, sid: str, profile: GovernanceProfile) -> None:
    """Category C seam: attach a profile without going through session_start."""
    sm._sessions[sid].profile = profile  # noqa: SLF001 - deliberate test seam


def _bind_via_resolution(tmp_path: Path, profile_file: Path, default_text: str = VALID_DEFAULT):
    res = tmp_path / "resolution.yaml"
    res.write_text(f"schema_version: 1\nbindings:\n  - agent_id: 'agent-*'\n    profile: {profile_file}\n", encoding="utf-8")
    default = tmp_path / "profile.yaml"
    default.write_text(default_text, encoding="utf-8")
    return resolve_profile(agent_id="agent-1", resolution_path=res, default_path=default), default


# ---------------------------------------------------------------------------
# Corpus integrity
# ---------------------------------------------------------------------------

def test_corpus_has_62_fixtures_in_three_file_categories():
    assert len(FIXTURES) == 62
    assert {f["category"] for f in FIXTURES} == {"A", "B", "F"}
    for fx in FIXTURES:
        assert _path(fx).is_file(), fx["file"]


# ---------------------------------------------------------------------------
# Category A: error-level, rejected at binding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fx", BY_CAT["A"], ids=IDS("A"))
def test_A_loads_and_validate_names_the_field(fx):
    profile = GovernanceProfile.from_file(_path(fx))  # parseable: loads
    result = profile.validate()
    assert not result.is_valid, "an error-level fixture must fail validate()"
    assert any(fx["expected"]["error_field"] in e for e in result.errors), result.errors


@pytest.mark.parametrize("fx", BY_CAT["A"], ids=IDS("A"))
def test_A_bound_profile_resolves_degraded_to_the_default(fx, tmp_path):
    resolved, default = _bind_via_resolution(tmp_path, _path(fx))
    assert resolved.source == SOURCE_DEGRADED
    assert resolved.binding == "agent-*"
    assert resolved.profile is not None
    assert resolved.profile.fingerprint() == GovernanceProfile.from_file(default).fingerprint()
    assert any(fx["file"] in w for w in resolved.warnings), resolved.warnings


@pytest.mark.parametrize("fx", BY_CAT["A"], ids=IDS("A"))
def test_A_session_start_refuses_to_activate_and_warns_once(fx, caplog):
    profile = GovernanceProfile.from_file(_path(fx))
    sm = SessionManager()
    with caplog.at_level(logging.WARNING):
        sm.session_start("s-refuse", "agent", profile=profile)
    assert sm.get_profile("s-refuse") is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "s-refuse" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("fx", BY_CAT["A"], ids=IDS("A"))
def test_A_forced_runtime_completes_without_manufactured_policy(fx):
    """Category C for every A fixture: the builder's defensive path on its own."""
    profile = GovernanceProfile.from_file(_path(fx))
    sm, cache = SessionManager(), InProcessCache()
    sm.session_start("s-forced", "agent"); cache.init_session("s-forced")
    _force_profile(sm, "s-forced", profile)
    reg, scopes = _governed_run(sm, cache, "s-forced")  # must not raise
    exp = fx["expected"]["runtime"]
    flags = {f for ev in scopes for f in ev.advisory_flags}
    assert (AdvisoryFlag.TASK_BOUNDARY_CROSSED in flags) is exp["task_boundary_flag"], flags
    assert (AdvisoryFlag.HIGH_CONSEQUENCE_DETECTED in flags) is exp["high_consequence_flag"], flags
    if "schema_version_recorded" in fx["expected"]:
        assert reg.payload.profile_schema_version == fx["expected"]["schema_version_recorded"]
    if fx["expected"].get("pol001_on_write"):
        # A list-of-pairs session_intent must not become `demand_at: never`.
        write_event = scopes[1]
        assert PolicyViolation.POL_001 in write_event.policy_violations


@pytest.mark.parametrize("fx", [f for f in BY_CAT["A"] if f["class"] == "tools-entry-type"], ids=lambda f: f["file"])
def test_C_tolerance_warns_once_per_session_per_field(fx, caplog):
    profile = GovernanceProfile.from_file(_path(fx))
    sm, cache = SessionManager(), InProcessCache()
    sm.session_start("s-once", "agent"); cache.init_session("s-once")
    _force_profile(sm, "s-once", profile)
    with caplog.at_level(logging.WARNING):
        _governed_run(sm, cache, "s-once")
    tool_warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "tools" in r.getMessage()]
    assert len(tool_warnings) == 1, [r.getMessage() for r in tool_warnings]


# ---------------------------------------------------------------------------
# Category B: warning-only, acceptable under current semantics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fx", BY_CAT["B"], ids=IDS("B"))
def test_B_loads_with_no_errors_and_at_least_one_warning(fx):
    result = GovernanceProfile.from_file(_path(fx)).validate()
    assert result.errors == []
    assert any(fx["expected"]["warning_substring"] in w for w in result.warnings), result.warnings


@pytest.mark.parametrize("fx", BY_CAT["B"], ids=IDS("B"))
def test_B_binds_and_runs(fx, tmp_path):
    resolved, _ = _bind_via_resolution(tmp_path, _path(fx))
    assert resolved.source == SOURCE_BOUND
    sm, cache = SessionManager(), InProcessCache()
    sm.session_start("s-b", "agent", profile=resolved.profile); cache.init_session("s-b")
    assert sm.get_profile("s-b") is resolved.profile
    _governed_run(sm, cache, "s-b")


# ---------------------------------------------------------------------------
# Category F: construction-rejected (D9)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fx", BY_CAT["F"], ids=IDS("F"))
def test_F_from_file_raises_the_contracted_exception(fx):
    exc = {"TypeError": TypeError, "ValueError": ValueError}[fx["expected"]["construction_raises"]]
    with pytest.raises(exc):
        GovernanceProfile.from_file(_path(fx))


@pytest.mark.parametrize("fx", BY_CAT["F"], ids=IDS("F"))
def test_F_direct_construction_raises_the_same_exception(fx):
    exc = {"TypeError": TypeError, "ValueError": ValueError}[fx["expected"]["construction_raises"]]
    data = yaml.safe_load(_path(fx).read_text(encoding="utf-8"))
    with pytest.raises(exc):
        GovernanceProfile(data)


@pytest.mark.parametrize("fx", BY_CAT["F"], ids=IDS("F"))
def test_F_bound_profile_resolves_degraded_with_a_load_failure_naming_the_file(fx, tmp_path):
    resolved, default = _bind_via_resolution(tmp_path, _path(fx))
    assert resolved.source == SOURCE_DEGRADED
    assert resolved.profile is not None and resolved.profile.fingerprint() == GovernanceProfile.from_file(default).fingerprint()
    assert any(fx["file"] in w for w in resolved.warnings), resolved.warnings


# ---------------------------------------------------------------------------
# D2-ii: a malformed DEFAULT fails open at the resolver
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fx", [BY_CAT["A"][0], BY_CAT["A"][8], BY_CAT["F"][0], BY_CAT["F"][8]],
                         ids=lambda f: "default_is_" + f["file"])
def test_D2_malformed_default_resolves_none_with_a_warning(fx, tmp_path):
    res = tmp_path / "resolution.yaml"  # absent: no bindings
    resolved = resolve_profile(agent_id="agent-1", resolution_path=res, default_path=_path(fx))
    assert resolved.source == SOURCE_NONE
    assert resolved.profile is None
    assert any(fx["file"] in w for w in resolved.warnings), resolved.warnings


# ---------------------------------------------------------------------------
# D3: registration evidence stays truthful after a refusal
# ---------------------------------------------------------------------------

def test_D3_refused_direct_profile_yields_truthful_registration():
    profile = GovernanceProfile.from_file(CORPUS_DIR / "tools_ints.yaml")
    sm, cache = SessionManager(), InProcessCache()
    sm.session_start("s-truth", "agent", profile=profile); cache.init_session("s-truth")
    reg, _ = _governed_run(sm, cache, "s-truth", profile_resolution=SOURCE_BOUND, profile_binding="agent-*")
    assert reg.payload.profile_loaded is None
    assert reg.payload.profile_schema_version is None
    assert reg.payload.profile_resolution == SOURCE_BOUND      # the caller's claim, recorded as claimed
    assert reg.payload.profile_binding == "agent-*"
    assert reg.profile_fingerprint is None                     # nothing active, nothing fingerprinted


# ---------------------------------------------------------------------------
# Valid-profile identity is untouched by any of the above
# ---------------------------------------------------------------------------

def test_valid_default_fixture_is_valid_binds_and_has_a_fingerprint(tmp_path):
    p = tmp_path / "ok.yaml"; p.write_text(VALID_DEFAULT, encoding="utf-8")
    profile = GovernanceProfile.from_file(p)
    assert profile.validate().is_valid
    assert len(profile.fingerprint()) == 12
    sm = SessionManager(); sm.session_start("s-ok", "agent", profile=profile)
    assert sm.get_profile("s-ok") is profile
