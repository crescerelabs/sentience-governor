"""v0.3.2 CP1 — ``high_consequence.operations`` schema and validation.

CP1 owns the field's default, its accepted shape, the domain/action
vocabularies, ``destructive`` validation, and the warnings for malformed or
unknown-vocabulary rules. Evaluation of the rules lands in a later
checkpoint; nothing here asserts runtime behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sentience_governor.profile.loader import GovernanceProfile
from sentience_governor.profile.schema import OPERATION_ACTIONS, OPERATION_DOMAINS


def _profile(tmp_path: Path, operations) -> GovernanceProfile:
    data = {"schema_version": 1, "high_consequence": {"operations": operations}}
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return GovernanceProfile.from_file(path)


def test_vocabularies_match_the_locked_plan():
    assert OPERATION_DOMAINS == {
        "filesystem", "version_control", "packages", "network",
        "cloud_infrastructure", "process", "unknown",
    }
    assert OPERATION_ACTIONS == {"read", "create", "modify", "delete", "execute", "unknown"}


def test_absent_operations_defaults_to_empty_list_and_validates_clean(tmp_path: Path):
    path = tmp_path / "p.yaml"
    path.write_text("schema_version: 1\n", encoding="utf-8")
    profile = GovernanceProfile.from_file(path)
    assert profile.high_consequence["operations"] == []
    result = profile.validate()
    assert result.is_valid
    assert not [w for w in result.warnings if "operations" in w]


@pytest.mark.parametrize(
    "rules",
    [
        [{"domain": "cloud_infrastructure", "destructive": True}],
        [{"domain": "filesystem", "action": "delete"}],
        [{"domain": ["network", "packages"], "action": ["modify", "delete"], "destructive": False}],
        [{"domain": "unknown"}],
        [{"action": "unknown"}],
    ],
)
def test_well_formed_rules_validate_without_warnings(tmp_path: Path, rules):
    result = _profile(tmp_path, rules).validate(strict=True)
    assert result.is_valid, result.errors
    assert not [w for w in result.warnings if "operations" in w]


def test_non_list_operations_is_an_error(tmp_path: Path):
    result = _profile(tmp_path, {"domain": "filesystem"}).validate()
    assert not result.is_valid
    assert any("operations' must be a list" in e for e in result.errors)


def test_non_mapping_rule_warns_and_errors_in_strict(tmp_path: Path):
    lenient = _profile(tmp_path, ["filesystem"]).validate()
    assert lenient.is_valid
    assert any("operations'[0] must be a mapping" in w for w in lenient.warnings)
    strict = _profile(tmp_path, ["filesystem"]).validate(strict=True)
    assert not strict.is_valid


def test_unknown_rule_key_warns_and_names_the_key(tmp_path: Path):
    result = _profile(tmp_path, [{"domain": "filesystem", "severity": "high"}]).validate()
    assert result.is_valid
    assert any("unknown key 'severity'" in w and "skipped at runtime" in w for w in result.warnings)


@pytest.mark.parametrize(
    "rule, fragment",
    [
        ({"domain": "cloud"}, "domain value 'cloud' is not recognized"),
        ({"action": "destroy"}, "action value 'destroy' is not recognized"),
        ({"domain": ["filesystem", "disk"]}, "domain value 'disk' is not recognized"),
        ({"domain": 7}, "domain value 7 is not recognized"),
    ],
)
def test_unknown_vocabulary_warns(tmp_path: Path, rule, fragment):
    result = _profile(tmp_path, [rule]).validate()
    assert result.is_valid
    assert any(fragment in w for w in result.warnings), result.warnings


def test_destructive_must_be_boolean(tmp_path: Path):
    for bad in ("yes", 1, None):
        result = _profile(tmp_path, [{"domain": "filesystem", "destructive": bad}]).validate()
        assert result.is_valid
        assert any("destructive must be true or false" in w for w in result.warnings), bad


def test_empty_rule_warns_that_it_matches_everything(tmp_path: Path):
    result = _profile(tmp_path, [{}]).validate()
    assert result.is_valid
    assert any("no predicates" in w for w in result.warnings)


def test_export_import_roundtrip_preserves_operations_and_identity(tmp_path: Path):
    profile = _profile(tmp_path, [{"domain": "filesystem", "action": "delete"}])
    out = tmp_path / "exported.yaml"
    profile.export(out)
    reloaded = GovernanceProfile.from_file(out)
    assert reloaded.high_consequence["operations"] == [{"domain": "filesystem", "action": "delete"}]
    assert reloaded.content_hash() == profile.content_hash()
