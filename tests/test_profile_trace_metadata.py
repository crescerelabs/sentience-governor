"""Tests for v0.2.5 CP3 — profile metadata in trace.

Covers:

* ``GovernanceEvent.profile_fingerprint`` — envelope-level field
  populated on every event when the session was started under an
  operator-authored profile (source_path set). None-omitted on
  serialization so v0.2.4 traces stay byte-identical.
* ``AgentRegisteredPayload.profile_loaded`` /
  ``profile_schema_version`` — payload-level fields on AGENT_REGISTERED
  only. Same None-omission rule.
* Wrapper helper ``GovernanceProfile.from_default_path_or_none`` —
  returns None when no file exists, returns the loaded profile when
  it does.

Strategy: instantiate the EventBuilder + SessionManager directly
with a constructed profile (no filesystem). One test exercises the
wrapper helper via monkeypatched DEFAULT_PROFILE_PATH.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile import GovernanceProfile, SCHEMA_VERSION
from sentience_governor.profile.schema import default_profile_data
from sentience_governor.schema.events import (
    DeploymentMode,
    OperationType,
)
from sentience_governor.session_manager.manager import SessionManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _profile_from_disk(tmp_path: Path) -> GovernanceProfile:
    """Write a minimal valid profile to disk and load it.

    The on-disk path provides ``source_path``, which is the signal
    EventBuilder uses to distinguish "operator-authored profile
    loaded" from "running on defaults."
    """
    yaml_text = (
        "schema_version: 1\n"
        "session_intent:\n"
        "  required: true\n"
        "  demand_at: session_start\n"
    )
    p = tmp_path / "profile.yaml"
    p.write_text(yaml_text)
    return GovernanceProfile.from_file(p)


def _make_builder(
    profile: GovernanceProfile | None,
    *,
    session_id: str = "sess",
    agent_id: str = "agent",
):
    sm = SessionManager()
    cache = InProcessCache()
    sm.session_start(session_id=session_id, agent_id=agent_id, profile=profile)
    cache.init_session(session_id)
    builder = EventBuilder(
        session_manager=sm,
        cache=cache,
        agent_id=agent_id,
        session_id=session_id,
        deployment_mode=DeploymentMode.vendor_managed,
    )
    return sm, cache, builder


# ---------------------------------------------------------------------------
# AGENT_REGISTERED payload-level fields
# ---------------------------------------------------------------------------


def test_agent_registered_no_profile_omits_metadata_fields():
    """Backward-compat: no profile → profile_loaded /
    profile_schema_version absent from serialized payload."""
    _, _, builder = _make_builder(profile=None)
    event = builder.build_agent_registered(
        agent_version="1.0",
        vendor_id="v1",
        declared_capabilities=["fs.write"],
        owner_claim="u1",
    )
    dumped = event.to_dict()
    assert "profile_loaded" not in dumped["payload"]
    assert "profile_schema_version" not in dumped["payload"]


def test_agent_registered_with_defaults_profile_still_omits_metadata():
    """Defaults profile (no source_path) → fields still absent.

    Defaults-only profiles (used internally for testing or fallback)
    do not count as an operator-authored profile, so they leave the
    trace shape unchanged.
    """
    defaults = GovernanceProfile.defaults()
    _, _, builder = _make_builder(profile=defaults)
    event = builder.build_agent_registered(
        agent_version="1.0",
        vendor_id="v1",
        declared_capabilities=["fs.write"],
        owner_claim="u1",
    )
    dumped = event.to_dict()
    assert "profile_loaded" not in dumped["payload"]
    assert "profile_schema_version" not in dumped["payload"]


def test_agent_registered_with_loaded_profile_populates_metadata(tmp_path):
    """Profile with source_path → profile_loaded=True + schema_version
    populated."""
    profile = _profile_from_disk(tmp_path)
    _, _, builder = _make_builder(profile=profile)
    event = builder.build_agent_registered(
        agent_version="1.0",
        vendor_id="v1",
        declared_capabilities=["fs.write"],
        owner_claim="u1",
    )
    dumped = event.to_dict()
    assert dumped["payload"]["profile_loaded"] is True
    assert dumped["payload"]["profile_schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Envelope-level profile_fingerprint
# ---------------------------------------------------------------------------


def test_envelope_no_profile_omits_fingerprint():
    """Backward-compat: no profile → profile_fingerprint absent."""
    _, _, builder = _make_builder(profile=None)
    event = builder.build_agent_registered(
        agent_version="1.0",
        vendor_id="v1",
        declared_capabilities=["fs.write"],
        owner_claim="u1",
    )
    dumped = event.to_dict()
    assert "profile_fingerprint" not in dumped


def test_envelope_with_loaded_profile_populates_fingerprint(tmp_path):
    """Profile with source_path → profile_fingerprint present on the
    AGENT_REGISTERED envelope."""
    profile = _profile_from_disk(tmp_path)
    _, _, builder = _make_builder(profile=profile)
    event = builder.build_agent_registered(
        agent_version="1.0",
        vendor_id="v1",
        declared_capabilities=["fs.write"],
        owner_claim="u1",
    )
    dumped = event.to_dict()
    assert "profile_fingerprint" in dumped
    assert dumped["profile_fingerprint"] == profile.fingerprint()
    # Fingerprint is the first 12 chars of the content hash.
    assert len(dumped["profile_fingerprint"]) == 12
    assert dumped["profile_fingerprint"] == profile.content_hash()[:12]


def test_fingerprint_present_on_every_event_under_profile(tmp_path):
    """Every event in a profile-loaded session carries the same
    fingerprint — immutable for the session's lifetime."""
    profile = _profile_from_disk(tmp_path)
    expected_fp = profile.fingerprint()
    _, _, builder = _make_builder(profile=profile)

    e1 = builder.build_agent_registered(
        agent_version="1.0",
        vendor_id="v1",
        declared_capabilities=["fs.write"],
        owner_claim="u1",
    )
    e2 = builder.build_scope_asserted(
        tool_id="fs.write",
        asserted_permissions=["write"],
        target_system="a.py",
        operation_type=OperationType.WRITE,
    )
    for ev in (e1, e2):
        dumped = ev.to_dict()
        assert dumped["profile_fingerprint"] == expected_fp


# ---------------------------------------------------------------------------
# Wrapper helper: from_default_path_or_none
# ---------------------------------------------------------------------------


def test_from_default_path_or_none_returns_none_when_no_file(
    monkeypatch, tmp_path
):
    """Helper returns None when ~/.sentience/profile.yaml absent —
    this is the signal wrappers use to take the v0.2.4 code path."""
    from sentience_governor.profile import loader as _loader

    # Point DEFAULT_PROFILE_PATH at a guaranteed-nonexistent file.
    monkeypatch.setattr(_loader, "DEFAULT_PROFILE_PATH", tmp_path / "nope.yaml")
    assert GovernanceProfile.from_default_path_or_none() is None


def test_from_default_path_or_none_returns_profile_when_file_exists(
    monkeypatch, tmp_path
):
    """Helper returns the loaded profile when the file exists.

    Verifies source_path is set on the returned profile (required
    for EventBuilder to distinguish operator-authored vs defaults).
    """
    from sentience_governor.profile import loader as _loader

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "schema_version: 1\n"
        "session_intent:\n"
        "  demand_at: first_write\n"
    )
    monkeypatch.setattr(_loader, "DEFAULT_PROFILE_PATH", profile_path)

    loaded = GovernanceProfile.from_default_path_or_none()
    assert loaded is not None
    assert loaded.source_path == profile_path
