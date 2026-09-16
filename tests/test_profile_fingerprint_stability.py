"""v0.3.2 CP1 — profile fingerprint stability and identity contract.

The fingerprint represents the profile's EFFECTIVE governance posture. A
new optional capability (``high_consequence.operations``) must not change
the identity of a profile that does not use it, and the semantically
unordered ``operations`` collection must not change identity when its
rules are reordered or duplicated. Historical ordered fields keep their
existing order-sensitive behaviour. See the locked v0.3.2 plan §3-§4.

The legacy fixtures under ``tests/fixtures/profiles/`` were written before
v0.3.2, and ``recorded_fingerprints_0.3.1.2.json`` holds the fingerprints
and full content hashes they produced under the v0.3.1.2 loader, computed
at public ``main`` ``50a1174`` BEFORE any v0.3.2 change was applied. That
file is the compatibility contract; it must never be regenerated from a
later loader.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from sentience_governor.profile.loader import (
    FINGERPRINT_LENGTH,
    GovernanceProfile,
    canonical_profile_data,
)
from sentience_governor.profile.schema import (
    DEFAULT_HIGH_CONSEQUENCE,
    OPTIONAL_ADDITIVE_FIELDS,
    SECTION_HIGH_CONSEQUENCE,
    SET_VALUED_RULE_PREDICATES,
    default_profile_data,
)

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"
RECORDED = json.loads((FIXTURES / "recorded_fingerprints_0.3.1.2.json").read_text())

R1 = {"domain": "cloud_infrastructure", "destructive": True}
R2 = {"domain": "filesystem", "action": "delete"}

BASE = {
    "schema_version": 1,
    "session_intent": {"demand_at": "first_write"},
    "task_boundary": {"signals": ["dir_change", "time_gap"]},
    "high_consequence": {"tools": ["Bash:shell", "fs.write:.*outside_project.*"]},
}


def _write(tmp_path: Path, data: dict, name: str = "p.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _with_ops(ops, base: dict = BASE) -> dict:
    data = copy.deepcopy(base)
    data["high_consequence"]["operations"] = ops
    return data


def _fp(tmp_path: Path, data: dict, name: str = "p.yaml") -> str:
    return GovernanceProfile.from_file(_write(tmp_path, data, name)).fingerprint()


# ---------------------------------------------------------------------------
# Historical stability: checked-in pre-v0.3.2 fixtures (plan §3, row 36)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    sorted(p.name for p in FIXTURES.glob("legacy_*.yaml")),
)
def test_legacy_fixture_keeps_recorded_0_3_1_2_identity(name: str):
    profile = GovernanceProfile.from_file(FIXTURES / name)
    assert profile.fingerprint() == RECORDED[name]["fingerprint"], name
    assert profile.content_hash() == RECORDED[name]["content_hash"], name


def test_defaults_keep_recorded_0_3_1_2_identity():
    assert GovernanceProfile.defaults().fingerprint() == RECORDED["defaults"]["fingerprint"]
    assert GovernanceProfile.defaults().content_hash() == RECORDED["defaults"]["content_hash"]


def test_the_new_default_field_is_present_in_merged_data_but_not_in_identity():
    """The drift the plan measured (§3.2), proven absent."""
    merged = GovernanceProfile.from_file(FIXTURES / "legacy_representative.yaml").to_dict()
    assert merged[SECTION_HIGH_CONSEQUENCE]["operations"] == []
    canonical = canonical_profile_data(merged)
    assert "operations" not in canonical[SECTION_HIGH_CONSEQUENCE]


# ---------------------------------------------------------------------------
# Identity contract (plan §4, row 37)
# ---------------------------------------------------------------------------


def test_content_hash_is_full_sha256_and_fingerprint_is_its_prefix(tmp_path: Path):
    profile = GovernanceProfile.from_file(_write(tmp_path, BASE))
    digest = profile.content_hash()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert profile.fingerprint() == digest[:FINGERPRINT_LENGTH]
    assert len(profile.fingerprint()) == 12
    # One canonical representation feeds both.
    assert digest == hashlib.sha256(profile.canonical_bytes()).hexdigest()


def test_explicit_empty_operations_equals_absent(tmp_path: Path):
    assert _fp(tmp_path, _with_ops([]), "a.yaml") == _fp(tmp_path, BASE, "b.yaml")


def test_null_operations_equals_absent(tmp_path: Path):
    assert _fp(tmp_path, _with_ops(None), "a.yaml") == _fp(tmp_path, BASE, "b.yaml")


def test_one_rule_changes_identity(tmp_path: Path):
    assert _fp(tmp_path, _with_ops([R1]), "a.yaml") != _fp(tmp_path, BASE, "b.yaml")


def test_rule_value_change_changes_identity(tmp_path: Path):
    changed = dict(R1, destructive=False)
    assert _fp(tmp_path, _with_ops([R1]), "a.yaml") != _fp(tmp_path, _with_ops([changed]), "b.yaml")


def test_rule_added_or_removed_changes_identity(tmp_path: Path):
    one = _fp(tmp_path, _with_ops([R1]), "a.yaml")
    two = _fp(tmp_path, _with_ops([R1, R2]), "b.yaml")
    assert one != two


def test_reordered_rules_do_not_change_identity(tmp_path: Path):
    assert _fp(tmp_path, _with_ops([R1, R2]), "a.yaml") == _fp(tmp_path, _with_ops([R2, R1]), "b.yaml")


def test_duplicate_rule_does_not_change_identity(tmp_path: Path):
    assert _fp(tmp_path, _with_ops([R1, R2]), "a.yaml") == _fp(tmp_path, _with_ops([R1, R2, R1]), "b.yaml")


def test_predicate_member_order_and_duplicates_do_not_change_identity(tmp_path: Path):
    a = _fp(tmp_path, _with_ops([{"domain": ["cloud_infrastructure", "network"]}]), "a.yaml")
    b = _fp(tmp_path, _with_ops([{"domain": ["network", "cloud_infrastructure"]}]), "b.yaml")
    c = _fp(
        tmp_path,
        _with_ops([{"domain": ["cloud_infrastructure", "network", "cloud_infrastructure"]}]),
        "c.yaml",
    )
    assert a == b == c


def test_predicate_member_change_changes_identity(tmp_path: Path):
    a = _fp(tmp_path, _with_ops([{"action": ["delete", "modify"]}]), "a.yaml")
    b = _fp(tmp_path, _with_ops([{"action": ["delete", "create"]}]), "b.yaml")
    assert a != b


def test_unknown_key_in_rule_changes_identity(tmp_path: Path):
    """Unknown keys participate in identity, as they already do at top level."""
    assert _fp(tmp_path, _with_ops([R1]), "a.yaml") != _fp(tmp_path, _with_ops([dict(R1, foo=1)]), "b.yaml")


def test_unknown_top_level_key_still_changes_identity(tmp_path: Path):
    data = copy.deepcopy(BASE)
    data["mystery"] = 1
    assert _fp(tmp_path, data, "a.yaml") != _fp(tmp_path, BASE, "b.yaml")


def test_historical_ordered_fields_are_still_order_sensitive(tmp_path: Path):
    """signals and tools are NOT registered as sets; their order still matters."""
    signals_reordered = copy.deepcopy(BASE)
    signals_reordered["task_boundary"]["signals"] = ["time_gap", "dir_change"]
    tools_reordered = copy.deepcopy(BASE)
    tools_reordered["high_consequence"]["tools"] = list(reversed(BASE["high_consequence"]["tools"]))
    base = _fp(tmp_path, BASE, "base.yaml")
    assert _fp(tmp_path, signals_reordered, "s.yaml") != base
    assert _fp(tmp_path, tools_reordered, "t.yaml") != base


def test_registry_covers_exactly_the_new_optional_field():
    """Guards against silently registering a historical field (which would
    change every existing fingerprint)."""
    assert OPTIONAL_ADDITIVE_FIELDS == {(SECTION_HIGH_CONSEQUENCE, "operations"): []}
    assert SET_VALUED_RULE_PREDICATES == frozenset({"domain", "action"})
    assert DEFAULT_HIGH_CONSEQUENCE["operations"] == []
    assert default_profile_data()[SECTION_HIGH_CONSEQUENCE]["operations"] == []


# ---------------------------------------------------------------------------
# Canonicalization properties the snapshot machinery (CP2) will rely on
# ---------------------------------------------------------------------------


def test_canonical_profile_data_is_pure_and_idempotent():
    merged = default_profile_data()
    merged[SECTION_HIGH_CONSEQUENCE]["operations"] = [R2, R1, R2]
    before = copy.deepcopy(merged)
    once = canonical_profile_data(merged)
    assert merged == before, "must not mutate its input"
    twice = canonical_profile_data(once)
    assert once == twice
    assert json.dumps(once, sort_keys=True) == json.dumps(twice, sort_keys=True)


def test_canonical_rules_are_sorted_deduplicated_and_readable():
    merged = default_profile_data()
    merged[SECTION_HIGH_CONSEQUENCE]["operations"] = [
        R2,
        {"domain": ["network", "cloud_infrastructure", "network"], "destructive": True},
        R2,
    ]
    ops = canonical_profile_data(merged)[SECTION_HIGH_CONSEQUENCE]["operations"]
    # Ordered by each rule's sort-keyed JSON serialization ("action" < "destructive"),
    # deduplicated, with the set-valued predicate sorted and deduplicated.
    assert ops == [
        {"action": "delete", "domain": "filesystem"},
        {"destructive": True, "domain": ["cloud_infrastructure", "network"]},
    ]
    assert all(isinstance(rule, dict) for rule in ops), "rules stay mappings, not encoded strings"


def test_round_trip_through_canonical_bytes_reproduces_identity(tmp_path: Path):
    """Rebuilding a profile from its own canonical bytes yields the same hash."""
    profile = GovernanceProfile.from_file(_write(tmp_path, _with_ops([R2, R1])))
    rebuilt = GovernanceProfile(json.loads(profile.canonical_bytes()), source_path=profile.source_path)
    assert rebuilt.content_hash() == profile.content_hash()
    assert rebuilt.canonical_bytes() == profile.canonical_bytes()
