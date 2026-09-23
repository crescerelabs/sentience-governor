"""Suite-wide isolation of core's profile-resolution paths.

Core computes ``DEFAULT_RESOLUTION_PATH`` and ``DEFAULT_PROFILE_PATH`` from
``Path.home()`` **at import time**, so the per-module ``isolated_home``
fixtures that redirect ``HOME`` do not redirect resolution. Since 0.1.1 the
capability resolves a profile at every session open, which makes this
fixture load-bearing: without it a developer's or operator's real
``~/.sentience/resolution.yaml`` or ``~/.sentience/profile.yaml`` would bind
into the suite. The pattern is the one core's own tests use.
"""

from __future__ import annotations

import pytest

from sentience_governor.profile import loader as _loader


@pytest.fixture(autouse=True)
def isolated_resolution_paths(tmp_path, monkeypatch):
    """Point core's resolution and default-profile lookups at empty,
    per-test locations. Tests that need a binding or a default profile
    write files at these paths."""
    config = tmp_path / "sentience-config"
    config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_loader, "DEFAULT_RESOLUTION_PATH", config / "resolution.yaml")
    monkeypatch.setattr(_loader, "DEFAULT_PROFILE_PATH", config / "profile.yaml")
    return config
