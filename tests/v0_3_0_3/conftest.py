"""Shared fixtures for the v0.3.0.3 configuration-convergence suite.

Every test here maps to a numbered test in the locked v0.3.0.3 plan's §12
(the single normative test list); the number appears in each docstring.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from sentience_governor.cli import hook_config as hc


def make_exec(path: Path) -> str:
    """Create a real executable file (so §7.1 verification passes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def managed_entry(command: str) -> dict:
    """Exactly the canonical outer-entry shape (`_hook_entry()`)."""
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


def doc_with(events_to_entries: dict) -> dict:
    return {"hooks": dict(events_to_entries)}


def all_events(entry_factory) -> dict:
    return doc_with({ev: [entry_factory()] for ev in hc.GOVERNED_EVENTS})


def write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


@pytest.fixture
def binary(tmp_path):
    """The running install's hook binary: real, executable, verifiable."""
    return make_exec(tmp_path / "install" / "bin" / hc.HOOK_BASENAME)


@pytest.fixture
def other_binary(tmp_path):
    """A second live hook binary at a different path (a 'teammate' install)."""
    return make_exec(tmp_path / "other-install" / "bin" / hc.HOOK_BASENAME)


@pytest.fixture
def project(tmp_path):
    """A project directory OUTSIDE any git repository, so §6.2's exception
    keeps the local file in the starting directory: deterministic paths."""
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def shared_path(project):
    return project / ".claude" / "settings.json"


@pytest.fixture
def local_path(project):
    # Outside a git repo the resolved local path IS the starting directory's.
    resolved = hc.resolve_local_settings_path(project)
    assert resolved == project / ".claude" / "settings.local.json"
    return resolved


@pytest.fixture
def seam(project, binary):
    """Run the seam against `project` with the fixture binary."""
    def _run():
        return hc.converge(project, caller="seam",
                           binary_resolver=lambda: binary)
    return _run


@pytest.fixture
def init(project, binary):
    """Run init's convergence against `project` with the fixture binary."""
    def _run():
        return hc.converge(project, caller="init",
                           binary_resolver=lambda: binary)
    return _run


@pytest.fixture
def tty(monkeypatch):
    """Force the §9 TTY gate open so warnings are observable."""
    monkeypatch.setattr(hc, "_stderr_isatty", lambda: True)


@pytest.fixture
def no_tty(monkeypatch):
    """Force the §9 TTY gate closed (scripts / CI)."""
    monkeypatch.setattr(hc, "_stderr_isatty", lambda: False)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a temp dir so first-run state, skills installs and
    expanduser never touch the real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SENTIENCE_NO_FIRST_RUN_PROMPT", "1")
    return home
