"""Tests for the v0.2.5 governance profile loader and validator.

Checkpoint 1 of v0.2.5 — covers the standalone profile module's
load / validate / hash / export surface. Runtime integration
(session manager wiring, policy evaluator behavior change, new
advisory flag emission) is tested in Checkpoint 2.

Critical guarantees verified:

* Defaults preserve v0.2.4 behavior when no profile file present
* Validation is read-only — never mutates the source file
* Content hash is deterministic across whitespace/comment/order
  changes
* Lenient by default, strict on opt-in
* Reserved sections (extends, policies, custom_rules) are
  recognized but warned-and-ignored
* Reserved on_match values (prompt, block, deny) are recognized
  but warned; only 'flag' is valid in v0.2.5
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentience_governor.profile import (
    DEMAND_AT_FIRST_WRITE,
    DEMAND_AT_NEVER,
    DEMAND_AT_SESSION_START,
    GovernanceProfile,
    ON_MATCH_FLAG,
    SCHEMA_VERSION,
    SIGNAL_DIR_CHANGE,
    SIGNAL_FILE_TYPE_SHIFT,
)
from sentience_governor.profile.schema import default_profile_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write a YAML string to a temp file; return the path."""
    path = tmp_path / "profile.yaml"
    path.write_text(content, encoding="utf-8")
    return path


SAMPLE_FULL_PROFILE = """\
schema_version: 1

session_intent:
  required: true
  demand_at: first_write
  prompt_template: |
    Before you write or execute, declare what you're doing.

task_boundary:
  signals:
    - dir_change
    - file_type_shift
  time_gap_seconds: 300
  dir_change_depth: 2
  on_match: flag

high_consequence:
  tools:
    - "Bash:.*rm.*-rf.*"
    - "git.*push.*--force"
  on_match: flag
"""


# ---------------------------------------------------------------------------
# Test 1 — defaults
# ---------------------------------------------------------------------------


def test_defaults_returns_sensible_shape():
    """No profile file present → defaults preserve v0.2.4 behavior."""
    profile = GovernanceProfile.defaults()
    assert profile.schema_version == SCHEMA_VERSION
    # session_intent defaults must preserve v0.2.4 behavior:
    # demand_at defaults to session_start so POL-001 fires on every
    # write-class event until intent is declared (current runtime
    # behavior).
    assert profile.session_intent["demand_at"] == DEMAND_AT_SESSION_START
    assert profile.session_intent["required"] is True
    # task_boundary defaults: no signals = no boundary detection.
    assert profile.task_boundary["signals"] == []
    # high_consequence defaults: no patterns = no flags fire.
    assert profile.high_consequence["tools"] == []
    # source_path is None for defaults (didn't come from disk).
    assert profile.source_path is None


# ---------------------------------------------------------------------------
# Test 2 — load full populated YAML
# ---------------------------------------------------------------------------


def test_from_file_loads_full_populated_profile(tmp_path: Path):
    """Valid YAML with every section populated loads correctly."""
    path = _write_yaml(tmp_path, SAMPLE_FULL_PROFILE)
    profile = GovernanceProfile.from_file(path)

    assert profile.schema_version == 1
    assert profile.session_intent["demand_at"] == DEMAND_AT_FIRST_WRITE
    assert profile.session_intent["required"] is True
    assert "Before you write" in profile.session_intent["prompt_template"]
    assert profile.task_boundary["signals"] == [SIGNAL_DIR_CHANGE, SIGNAL_FILE_TYPE_SHIFT]
    assert profile.task_boundary["time_gap_seconds"] == 300
    assert profile.task_boundary["on_match"] == ON_MATCH_FLAG
    assert profile.high_consequence["tools"] == [
        "Bash:.*rm.*-rf.*",
        "git.*push.*--force",
    ]
    assert profile.source_path == path


# ---------------------------------------------------------------------------
# Test 3 — load partial YAML, defaults fill the gaps
# ---------------------------------------------------------------------------


def test_from_file_partial_profile_fills_defaults(tmp_path: Path):
    """Partial profile YAML merges with defaults for missing sections."""
    partial = """\
session_intent:
  demand_at: never
"""
    path = _write_yaml(tmp_path, partial)
    profile = GovernanceProfile.from_file(path)

    # Explicit field honored
    assert profile.session_intent["demand_at"] == DEMAND_AT_NEVER
    # Unspecified field gets default
    assert profile.session_intent["required"] is True
    # Sections not present in file get defaults
    assert profile.task_boundary["signals"] == []
    assert profile.high_consequence["tools"] == []


# ---------------------------------------------------------------------------
# Test 4 — invalid YAML raises clear error
# ---------------------------------------------------------------------------


def test_from_file_invalid_yaml_raises_value_error(tmp_path: Path):
    """Unparseable YAML produces a clear error, not a crash."""
    path = _write_yaml(tmp_path, "session_intent:\n  required: [bad\n")
    with pytest.raises(ValueError, match="Failed to parse profile YAML"):
        GovernanceProfile.from_file(path)


# ---------------------------------------------------------------------------
# Test 5 — strict validation rejects unknown top-level keys
# ---------------------------------------------------------------------------


def test_validate_strict_rejects_unknown_top_level_keys(tmp_path: Path):
    """In strict mode, unknown top-level keys become errors."""
    yaml_with_unknown = SAMPLE_FULL_PROFILE + "\nunknown_top_level: value\n"
    path = _write_yaml(tmp_path, yaml_with_unknown)
    profile = GovernanceProfile.from_file(path)

    result = profile.validate(strict=True)
    assert result.is_valid is False
    assert any("unknown_top_level" in err for err in result.errors)


# ---------------------------------------------------------------------------
# Test 6 — lenient validation warns on unknown but accepts
# ---------------------------------------------------------------------------


def test_validate_lenient_warns_on_unknown_top_level_keys(tmp_path: Path):
    """In lenient mode (default), unknown top-level keys produce warnings only."""
    yaml_with_unknown = SAMPLE_FULL_PROFILE + "\nfoo_bar_baz: value\n"
    path = _write_yaml(tmp_path, yaml_with_unknown)
    profile = GovernanceProfile.from_file(path)

    result = profile.validate(strict=False)
    assert result.is_valid is True  # warnings don't fail validation
    assert any("foo_bar_baz" in w for w in result.warnings)
    assert len(result.errors) == 0


# ---------------------------------------------------------------------------
# Test 7 — reserved on_match values warn but don't error
# ---------------------------------------------------------------------------


def test_validate_reserved_on_match_value_warns(tmp_path: Path):
    """on_match: prompt is reserved for paid-tier enforcement; warn,
    don't fail."""
    yaml_with_prompt = """\
schema_version: 1
high_consequence:
  tools: ["foo"]
  on_match: prompt
"""
    path = _write_yaml(tmp_path, yaml_with_prompt)
    profile = GovernanceProfile.from_file(path)

    result = profile.validate(strict=False)
    assert result.is_valid is True
    assert any("prompt" in w and "reserved" in w for w in result.warnings)
    # Block and deny should warn the same way.
    for reserved_value in ("block", "deny"):
        path2 = _write_yaml(
            tmp_path,
            f"schema_version: 1\nhigh_consequence:\n  on_match: {reserved_value}\n",
        )
        p = GovernanceProfile.from_file(path2)
        r = p.validate(strict=False)
        assert any(reserved_value in w and "reserved" in w for w in r.warnings), (
            f"Expected reserved warning for on_match={reserved_value}"
        )


# ---------------------------------------------------------------------------
# Test 8 — extends field recognized, ignored, warned
# ---------------------------------------------------------------------------


def test_validate_extends_field_warns_but_preserves(tmp_path: Path):
    """`extends` is reserved for future inheritance; warn + ignore."""
    yaml_with_extends = """\
schema_version: 1
extends: https://example.com/parent.yaml
session_intent:
  demand_at: first_write
"""
    path = _write_yaml(tmp_path, yaml_with_extends)
    profile = GovernanceProfile.from_file(path)

    result = profile.validate(strict=False)
    assert result.is_valid is True
    assert any("extends" in w and "reserved" in w for w in result.warnings)
    # Operator's extends value preserved in the data (validator
    # warns but loader doesn't strip it).
    assert profile.to_dict().get("extends") == "https://example.com/parent.yaml"


# ---------------------------------------------------------------------------
# Test 9 — policies field recognized, ignored, warned
# ---------------------------------------------------------------------------


def test_validate_policies_field_warns(tmp_path: Path):
    """`policies` is reserved for policy rule customization candidate."""
    yaml_with_policies = """\
schema_version: 1
policies:
  POL-001:
    enabled: true
"""
    path = _write_yaml(tmp_path, yaml_with_policies)
    profile = GovernanceProfile.from_file(path)

    result = profile.validate(strict=False)
    assert result.is_valid is True
    assert any("policies" in w and "reserved" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Test 10 — custom_rules field recognized, ignored, warned
# ---------------------------------------------------------------------------


def test_validate_custom_rules_field_warns(tmp_path: Path):
    """`custom_rules` is reserved for the custom-rules candidate."""
    yaml_with_custom = """\
schema_version: 1
custom_rules:
  - id: CUSTOM-001
    match: { tool_pattern: "sql.*" }
"""
    path = _write_yaml(tmp_path, yaml_with_custom)
    profile = GovernanceProfile.from_file(path)

    result = profile.validate(strict=False)
    assert result.is_valid is True
    assert any("custom_rules" in w and "reserved" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Test 11 — content hash deterministic across runs for identical content
# ---------------------------------------------------------------------------


def test_content_hash_deterministic_across_runs(tmp_path: Path):
    """Same profile content → same hash on repeated load."""
    path = _write_yaml(tmp_path, SAMPLE_FULL_PROFILE)
    p1 = GovernanceProfile.from_file(path)
    p2 = GovernanceProfile.from_file(path)
    assert p1.content_hash() == p2.content_hash()
    assert p1.fingerprint() == p2.fingerprint()
    assert len(p1.fingerprint()) == 12


# ---------------------------------------------------------------------------
# Test 12 — content hash changes when content changes
# ---------------------------------------------------------------------------


def test_content_hash_changes_on_content_change(tmp_path: Path):
    """Different profile content → different hashes."""
    p1 = GovernanceProfile.defaults()
    # Build a different profile by editing the data dict directly
    # (legitimate test path; not the normal user path).
    data2 = default_profile_data()
    data2["session_intent"]["demand_at"] = DEMAND_AT_FIRST_WRITE
    p2 = GovernanceProfile(data2)

    assert p1.content_hash() != p2.content_hash()
    assert p1.fingerprint() != p2.fingerprint()


# ---------------------------------------------------------------------------
# Test 13 — content hash stable across whitespace/comment/order
# ---------------------------------------------------------------------------


def test_content_hash_stable_across_whitespace_and_comments(tmp_path: Path):
    """Whitespace and comment-only changes in YAML must not change the hash.

    The canonical form is the JSON-serialized data dict with sorted
    keys, so any source-level cosmetic differences disappear before
    hashing.
    """
    yaml_a = """\
schema_version: 1
session_intent:
  required: true
  demand_at: first_write
"""
    yaml_b = """\
# This is a comment
schema_version: 1

session_intent:
  # Another comment
  demand_at: first_write
  required: true       # trailing comment with whitespace
"""
    path_a = tmp_path / "a.yaml"
    path_a.write_text(yaml_a, encoding="utf-8")
    path_b = tmp_path / "b.yaml"
    path_b.write_text(yaml_b, encoding="utf-8")

    p_a = GovernanceProfile.from_file(path_a)
    p_b = GovernanceProfile.from_file(path_b)

    assert p_a.content_hash() == p_b.content_hash(), (
        "Content hash must be stable across whitespace/comment/key-order differences."
    )


# ---------------------------------------------------------------------------
# Test 14 — export writes correct header
# ---------------------------------------------------------------------------


def test_export_writes_header_with_schema_version_and_hash(tmp_path: Path):
    """`export()` writes header lines for schema_version, content_hash, timestamp."""
    profile = GovernanceProfile.defaults()
    out_path = tmp_path / "exported.yaml"
    profile.export(out_path)

    text = out_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    # First three header lines (in this exact shape per the loader)
    assert lines[0] == f"# Schema version: {SCHEMA_VERSION}"
    assert lines[1].startswith("# Content hash: sha256:")
    assert lines[2].startswith("# Generated: ")
    # Hash in the header matches the profile's actual content hash.
    expected_hash = profile.content_hash()
    assert f"sha256:{expected_hash}" in text


# ---------------------------------------------------------------------------
# Test 15 — re-import after export produces equivalent profile
# ---------------------------------------------------------------------------


def test_export_then_import_roundtrip_preserves_content_hash(tmp_path: Path):
    """A profile exported and then re-imported has the same content hash.

    The header timestamp differs across runs, but the hash is over
    the canonical form of the data — not the YAML text — so it
    stays stable.
    """
    path_orig = _write_yaml(tmp_path, SAMPLE_FULL_PROFILE)
    p_orig = GovernanceProfile.from_file(path_orig)

    out_path = tmp_path / "exported.yaml"
    p_orig.export(out_path)

    p_reloaded = GovernanceProfile.from_file(out_path)
    assert p_orig.content_hash() == p_reloaded.content_hash()
    assert p_orig.to_dict() == p_reloaded.to_dict()


# ---------------------------------------------------------------------------
# Test 16 — validate is READ-ONLY (load-bearing acceptance criterion)
# ---------------------------------------------------------------------------


def test_validate_does_not_mutate_source_file(tmp_path: Path):
    """Acceptance criterion: validate() never writes to the source file.

    Verified by hashing the file bytes before and after validate
    invocations (lenient and strict). mtime is a softer signal
    (could be touched without content change); bytes-identical is
    the strict guarantee.
    """
    path = _write_yaml(tmp_path, SAMPLE_FULL_PROFILE)
    bytes_before = path.read_bytes()
    mtime_before = path.stat().st_mtime_ns

    profile = GovernanceProfile.from_file(path)

    # Run both modes; neither should touch the file.
    _ = profile.validate(strict=False)
    _ = profile.validate(strict=True)

    bytes_after = path.read_bytes()
    mtime_after = path.stat().st_mtime_ns

    assert bytes_before == bytes_after, (
        "validate() must be read-only; file bytes changed during validation."
    )
    assert mtime_before == mtime_after, (
        "validate() must be read-only; file mtime changed during validation."
    )
