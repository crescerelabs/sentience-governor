"""CP10 — the two distributions are independent, and each is gated.

These tests are about the *release ledger*, not the runtime. They answer:
does this distribution build to its own artifact, carry its own history, and
fail its own gate when it should?

The expensive proofs live in `scripts/release_check.py`, which builds and
inspects real artifacts. What is here is what a test suite can hold cheaply
and must never stop holding.
"""

from __future__ import annotations

import re
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no stdlib tomllib
    import tomli as tomllib
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
REPO = PKG.parent.parent
GATE = PKG / "scripts" / "release_check.py"

repo_only = pytest.mark.skipif(
    not (REPO / "pyproject.toml").exists(),
    reason="repository-only: the core tree is not part of the wheel")


def _version() -> str:
    with (PKG / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


# ---------------------------------------------------------------------------
# The ledger exists and is ours alone
# ---------------------------------------------------------------------------

def test_the_integration_has_its_own_changelog():
    assert (PKG / "CHANGELOG.md").exists()


def test_the_changelog_covers_the_current_version():
    assert f"[{_version()}]" in (PKG / "CHANGELOG.md").read_text()


@repo_only
def test_no_core_release_appears_in_our_changelog():
    """§5: neither distribution's releases appear in the other's ledger."""
    ours = (PKG / "CHANGELOG.md").read_text()
    core_versions = re.findall(r"^## \[(\d+\.\d+[\d.]*)\]",
                               (REPO / "CHANGELOG.md").read_text(), re.M)
    assert core_versions, "sanity: core changelog parsed"
    leaked = [v for v in core_versions if f"[{v}]" in ours]
    assert leaked == [], f"core releases named in our changelog: {leaked}"


@repo_only
def test_our_release_does_not_appear_in_the_core_changelogs():
    version = _version()
    for name in ("CHANGELOG.md", "docs/changelog.md"):
        path = REPO / name
        if not path.exists():
            continue
        hit = re.search(rf"\[{re.escape(version)}\][^\n]*pydantic",
                        path.read_text(), re.I)
        assert hit is None, f"our release leaked into {name}"


# ---------------------------------------------------------------------------
# Dependency direction, asserted from metadata rather than assumed
# ---------------------------------------------------------------------------

@repo_only
def test_core_metadata_names_no_pydantic_dependency():
    """The direction that must never invert.

    Core acquiring a Pydantic dependency, even an optional one, would make
    every core install carry this integration's surface.
    """
    with (REPO / "pyproject.toml").open("rb") as fh:
        core = tomllib.load(fh)["project"]
    deps = " ".join(core.get("dependencies", []))
    assert "pydantic-ai" not in deps.lower()
    for extra, items in (core.get("optional-dependencies") or {}).items():
        assert "pydantic-ai" not in " ".join(items).lower(), extra


def test_our_metadata_names_core_at_the_locked_bound():
    with (PKG / "pyproject.toml").open("rb") as fh:
        deps = " ".join(tomllib.load(fh)["project"]["dependencies"])
    assert "sentience-governor>=0.3.2,<0.3.3" in deps
    assert "pydantic-ai-slim>=2.37.0,<2.38" in deps


def test_the_keyword_list_does_not_claim_tracing():
    """CP10 metadata correction.

    The README spends a section distinguishing Agent Execution Evidence from
    logging, tracing and observability. A keyword saying "tracing" would put
    the package in the category the page distinguishes itself from, and
    keywords are how someone finds it before reading a word.
    """
    with (PKG / "pyproject.toml").open("rb") as fh:
        keywords = tomllib.load(fh)["project"]["keywords"]
    assert "execution-tracing" not in keywords
    assert "execution-evidence" in keywords


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------

@repo_only
def test_the_release_check_exists_and_is_executable():
    assert GATE.exists()
    assert GATE.read_text().startswith("#!/usr/bin/env python3")


@repo_only
def test_the_release_check_reads_the_single_version_authority():
    """CP1's authority, not a hand-maintained duplicate."""
    text = GATE.read_text()
    assert 'PKG / "pyproject.toml"' in text
    # Forbid READING a version from anywhere else. The phrase may appear in
    # prose explaining why it is not read; what must not appear is an actual
    # attribute access or an import of the package to ask it.
    assert ".__version__" not in text, (
        "the gate must not read a hand-maintained __version__")
    assert "import pydantic_ai_governor" not in text, (
        "the gate must read metadata, not import the package")


@repo_only
def test_the_release_check_fails_when_the_changelog_lacks_the_version():
    """The failure this gate exists to produce, proven by producing it.

    A version bumped without a changelog entry is the exact mistake the
    ledger is for. The changelog is restored in a `finally`, so nothing on
    disk is left modified.

    `--no-tests` is not a convenience here, it is required: gate 1 runs this
    suite, so invoking the full gate from inside it would re-enter the suite
    and never terminate. That recursion was found by hitting it.
    """
    changelog = PKG / "CHANGELOG.md"
    original = changelog.read_text()
    try:
        changelog.write_text(original.replace(f"[{_version()}]", "[9.9.9]"))
        result = subprocess.run(
            [sys.executable, str(GATE), "--no-build", "--no-tests"],
            capture_output=True, text=True, cwd=REPO)
        assert result.returncode != 0, "the gate passed with no changelog entry"
        assert "gate  7" in result.stdout or "gate 7" in result.stdout
    finally:
        changelog.write_text(original)


@repo_only
def test_the_release_check_does_not_touch_the_core_gate():
    """A CP10 non-goal, enforced.

    The core script keeps its contract and stays unaware of a second
    distribution.
    """
    core_gate = (REPO / "scripts" / "release_check.py").read_text()
    assert "pydantic" not in core_gate.lower()
