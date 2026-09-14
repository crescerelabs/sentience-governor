"""v0.3.2 CP1 — per-session policy resolution (``profile/resolver.py``).

Locked semantics (plan Rev 6 §4-of-Rev-3, §9): keyed on ``agent_id``; the
FIRST matching binding is authoritative; a matched binding whose profile
cannot be loaded is a DEGRADED resolution that never falls through to a
later binding and falls back to the default profile if present, else to
no profile; a missing, unparseable or malformed resolution file is
ignored with a warning; ``~`` and relative paths expand; the new layer
never raises; the default file keeps its existing behaviour.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from sentience_governor.profile.loader import GovernanceProfile
from sentience_governor.profile.resolver import (
    RESOLUTION_SCHEMA_VERSION,
    SOURCE_BOUND,
    SOURCE_DEFAULT,
    SOURCE_DEGRADED,
    SOURCE_NONE,
    ResolvedProfile,
    resolve_profile,
)


def _profile_file(path: Path, demand_at: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"schema_version": 1, "session_intent": {"demand_at": demand_at}}),
        encoding="utf-8",
    )
    return path


def _resolution_file(path: Path, bindings, schema_version=RESOLUTION_SCHEMA_VERSION) -> Path:
    doc = {"bindings": bindings}
    if schema_version is not None:
        doc["schema_version"] = schema_version
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path):
    """A fake ~/.sentience with three profiles and a default."""
    base = tmp_path / ".sentience"
    _profile_file(base / "profiles" / "strict.yaml", "session_start")
    _profile_file(base / "profiles" / "standard.yaml", "first_write")
    _profile_file(base / "profile.yaml", "never")
    return base


def _resolve(home: Path, agent_id: str, *, with_default: bool = True) -> ResolvedProfile:
    return resolve_profile(
        agent_id=agent_id,
        resolution_path=home / "resolution.yaml",
        default_path=home / "profile.yaml" if with_default else home / "absent.yaml",
    )


# ---------------------------------------------------------------------------
# No file / no match: existing behaviour preserved
# ---------------------------------------------------------------------------


def test_no_resolution_file_and_default_present_resolves_default(home: Path):
    r = _resolve(home, "claude-code-abc12345")
    assert r.source == SOURCE_DEFAULT
    assert r.binding is None
    assert r.profile is not None and r.profile.session_intent["demand_at"] == "never"
    assert r.profile.source_path == home / "profile.yaml"
    assert r.warnings == ()


def test_no_resolution_file_and_no_default_resolves_none(home: Path):
    r = _resolve(home, "claude-code-abc12345", with_default=False)
    assert r.source == SOURCE_NONE and r.profile is None and r.binding is None


def test_default_step_matches_from_default_path_or_none_semantics(home: Path, monkeypatch):
    """The resolver's default step must load exactly what the existing
    loader entry point loads, including source_path (which gates the
    fingerprint on events)."""
    from sentience_governor.profile import loader as loader_module

    monkeypatch.setattr(loader_module, "DEFAULT_PROFILE_PATH", home / "profile.yaml")
    legacy = GovernanceProfile.from_default_path_or_none()
    r = _resolve(home, "anyone")
    assert legacy is not None
    assert r.profile is not None
    assert r.profile.content_hash() == legacy.content_hash()
    assert r.profile.source_path == legacy.source_path


def test_unmatched_bindings_fall_to_default_with_no_warning(home: Path):
    _resolution_file(
        home / "resolution.yaml",
        [{"agent_id": "deploy-bot", "profile": "profiles/strict.yaml"}],
    )
    r = _resolve(home, "claude-code-abc12345")
    assert r.source == SOURCE_DEFAULT and r.binding is None and r.warnings == ()


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_exact_match_binds(home: Path):
    _resolution_file(home / "resolution.yaml", [{"agent_id": "deploy-bot", "profile": "profiles/strict.yaml"}])
    r = _resolve(home, "deploy-bot")
    assert r.source == SOURCE_BOUND
    assert r.binding == "deploy-bot"
    assert r.profile.session_intent["demand_at"] == "session_start"
    assert r.profile.source_path == home / "profiles" / "strict.yaml"
    assert r.warnings == ()


def test_glob_match_binds_and_is_case_sensitive(home: Path):
    _resolution_file(home / "resolution.yaml", [{"agent_id": "claude-code-*", "profile": "profiles/standard.yaml"}])
    assert _resolve(home, "claude-code-4ee05407").source == SOURCE_BOUND
    assert _resolve(home, "Claude-Code-4ee05407").source == SOURCE_DEFAULT


def test_first_matching_binding_wins(home: Path):
    _resolution_file(
        home / "resolution.yaml",
        [
            {"agent_id": "claude-code-*", "profile": "profiles/standard.yaml"},
            {"agent_id": "*", "profile": "profiles/strict.yaml"},
        ],
    )
    r = _resolve(home, "claude-code-4ee05407")
    assert r.binding == "claude-code-*"
    assert r.profile.session_intent["demand_at"] == "first_write"


# ---------------------------------------------------------------------------
# Degraded: first match is authoritative even when it fails
# ---------------------------------------------------------------------------


def test_failed_first_match_does_not_fall_through_to_later_binding(home: Path):
    _resolution_file(
        home / "resolution.yaml",
        [
            {"agent_id": "deploy-bot", "profile": "profiles/missing.yaml"},
            {"agent_id": "*", "profile": "profiles/standard.yaml"},
        ],
    )
    r = _resolve(home, "deploy-bot")
    assert r.source == SOURCE_DEGRADED
    assert r.binding == "deploy-bot"
    # fell back to the DEFAULT, not to the '*' binding
    assert r.profile.session_intent["demand_at"] == "never"
    assert any("degraded" in w and "later bindings are not consulted" in w for w in r.warnings)


def test_failed_match_without_default_degrades_to_none(home: Path):
    _resolution_file(
        home / "resolution.yaml",
        [
            {"agent_id": "deploy-bot", "profile": "profiles/missing.yaml"},
            {"agent_id": "*", "profile": "profiles/standard.yaml"},
        ],
    )
    r = _resolve(home, "deploy-bot", with_default=False)
    assert r.source == SOURCE_DEGRADED and r.profile is None and r.binding == "deploy-bot"


@pytest.mark.parametrize(
    "content",
    ["- not: a mapping\n", "session_intent: [\n", "just a string\n"],
)
def test_matched_binding_with_malformed_profile_degrades(home: Path, content: str):
    bad = home / "profiles" / "bad.yaml"
    bad.write_text(content, encoding="utf-8")
    _resolution_file(home / "resolution.yaml", [{"agent_id": "*", "profile": "profiles/bad.yaml"}])
    r = _resolve(home, "anyone")
    assert r.source == SOURCE_DEGRADED and r.profile is not None
    assert r.profile.session_intent["demand_at"] == "never"


# ---------------------------------------------------------------------------
# Resolution file problems: ignored with a warning, never a match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content, fragment",
    [
        ("bindings: [\n", "not valid YAML"),
        ("- a\n- b\n", "root must be a mapping"),
        ("schema_version: 2\nbindings: []\n", "schema_version=2"),
        ("bindings: {}\n", "schema_version=None"),
        ("schema_version: 1\nbindings: notalist\n", "'bindings' must be a list"),
    ],
)
def test_malformed_resolution_file_is_ignored_with_warning(home: Path, content: str, fragment: str):
    (home / "resolution.yaml").write_text(content, encoding="utf-8")
    r = _resolve(home, "anyone")
    assert r.source == SOURCE_DEFAULT and r.binding is None
    assert any(fragment in w for w in r.warnings), r.warnings


def test_empty_resolution_file_is_equivalent_to_absent(home: Path):
    (home / "resolution.yaml").write_text("", encoding="utf-8")
    r = _resolve(home, "anyone")
    assert r.source == SOURCE_DEFAULT and r.warnings == ()


def test_malformed_binding_entries_are_skipped_but_good_ones_still_apply(home: Path):
    _resolution_file(
        home / "resolution.yaml",
        [
            "not-a-mapping",
            {"agent_id": "", "profile": "profiles/strict.yaml"},
            {"agent_id": "deploy-bot"},
            {"agent_id": "deploy-bot", "profile": "profiles/strict.yaml", "extra": 1},
        ],
    )
    r = _resolve(home, "deploy-bot")
    assert r.source == SOURCE_BOUND
    assert len([w for w in r.warnings if "skipped" in w]) == 3
    assert any("unknown keys extra" in w and "still used" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# Path expansion
# ---------------------------------------------------------------------------


def test_relative_profile_path_resolves_against_resolution_file_directory(home: Path):
    _resolution_file(home / "resolution.yaml", [{"agent_id": "*", "profile": "./profiles/strict.yaml"}])
    assert _resolve(home, "x").profile.source_path == home / "profiles" / "strict.yaml"


def test_tilde_and_absolute_paths_expand(home: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(home.parent))
    _resolution_file(home / "resolution.yaml", [{"agent_id": "*", "profile": "~/.sentience/profiles/strict.yaml"}])
    assert _resolve(home, "x").profile.source_path == home / "profiles" / "strict.yaml"
    _resolution_file(home / "resolution.yaml", [{"agent_id": "*", "profile": str(home / "profiles" / "standard.yaml")}])
    assert _resolve(home, "x").profile.session_intent["demand_at"] == "first_write"


# ---------------------------------------------------------------------------
# Determinism and the never-raises contract
# ---------------------------------------------------------------------------


def test_resolution_is_deterministic(home: Path):
    _resolution_file(home / "resolution.yaml", [{"agent_id": "claude-code-*", "profile": "profiles/standard.yaml"}])
    a = _resolve(home, "claude-code-1")
    b = _resolve(home, "claude-code-1")
    assert (a.source, a.binding, a.profile.content_hash(), a.warnings) == (
        b.source, b.binding, b.profile.content_hash(), b.warnings,
    )


def test_unreadable_resolution_file_is_ignored(home: Path, monkeypatch):
    _resolution_file(home / "resolution.yaml", [{"agent_id": "*", "profile": "profiles/strict.yaml"}])
    original = Path.read_text

    def boom(self, *a, **k):
        if self.name == "resolution.yaml":
            raise OSError("simulated permission denied")
        return original(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    r = _resolve(home, "anyone")
    assert r.source == SOURCE_DEFAULT
    assert any("could not be read" in w for w in r.warnings)


def test_unexpected_exception_in_binding_layer_falls_back_to_default(home: Path, monkeypatch):
    from sentience_governor.profile import resolver as module

    monkeypatch.setattr(module, "_load_bindings", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = _resolve(home, "anyone")
    assert r.source == SOURCE_DEFAULT
    assert any("resolution failed unexpectedly" in w for w in r.warnings)


def test_malformed_default_profile_keeps_existing_raising_behaviour(home: Path):
    """Locked plan §4-of-Rev-3: the default step is preserved exactly,
    including the ValueError a malformed profile.yaml raises today via
    from_default_path_or_none. Only the NEW layer never raises."""
    (home / "profile.yaml").write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError):
        _resolve(home, "anyone")


def test_warnings_are_logged_once_each(home: Path, caplog):
    _resolution_file(home / "resolution.yaml", [{"agent_id": "deploy-bot", "profile": "profiles/missing.yaml"}])
    with caplog.at_level(logging.WARNING, logger="sentience_governor.profile.resolver"):
        r = _resolve(home, "deploy-bot")
    assert len(r.warnings) == 1
    assert sum("profile resolution" in rec.getMessage() for rec in caplog.records) == 1
