"""CP1 — the package exists, versions from one place, and is self-contained.

No capability behavior is tested here because none exists yet. What these
tests protect is the packaging contract: a single version authority, a
correct licence, and a suite that runs on its own config rather than the
repository root's.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import metadata, version
from pathlib import Path

import pydantic_ai_governor

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISTRIBUTION = "pydantic-ai-governor"


def _pyproject() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_package_imports():
    assert pydantic_ai_governor is not None


def test_version_is_derived_from_installed_metadata():
    """`__version__` must come from the distribution, not a literal."""
    assert pydantic_ai_governor.__version__ == version(DISTRIBUTION)


def test_version_matches_the_single_authority():
    """That distribution version must in turn be the pyproject value, so
    there is exactly one place a version is written."""
    assert version(DISTRIBUTION) == _pyproject()["project"]["version"]


def test_no_second_hand_maintained_version_number():
    """Guard against a version NUMBER creeping into the source.

    The not-installed sentinel is allowed: it is an admission of absence,
    not a second version authority. What must never appear is a literal
    that could disagree with the distribution, such as `= "0.1.0"`.
    """
    import re

    number = re.compile(r"""__version__\s*=\s*['"]\d""")
    offenders = []
    for path in (PROJECT_ROOT / "src" / "pydantic_ai_governor").rglob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if number.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, offenders


def test_licence_is_apache_2_0_in_the_pep_639_form():
    project = _pyproject()["project"]
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE"]
    assert metadata(DISTRIBUTION)["License-Expression"] == "Apache-2.0"


def test_licence_file_is_bundled_not_borrowed_from_the_repo_root():
    """The distribution must be self-contained: its own LICENSE, not the
    repository root's."""
    licence = PROJECT_ROOT / "LICENSE"
    assert licence.is_file()
    assert "Apache License" in licence.read_text()


def test_dependency_bounds_are_the_locked_ones():
    """The bounds are published compatibility contracts. Widening them is a
    deliberate act in a new release, never an edit in place."""
    deps = _pyproject()["project"]["dependencies"]
    assert "sentience-governor>=0.3.1.2,<0.3.2" in deps
    assert "pydantic-ai-slim>=2.37.0,<2.38" in deps


def test_dependency_direction_never_inverts():
    """Core must not depend on this package, or on Pydantic AI."""
    core = metadata("sentience-governor")
    requires = [r.lower() for r in (core.get_all("Requires-Dist") or [])]
    assert not [r for r in requires if "pydantic-ai" in r]
    assert not [r for r in requires if "pydantic-ai-governor" in r]


def test_pytest_config_is_self_contained():
    """Root `testpaths` points at the core suite. This package must carry
    its own, or its tests silently never run."""
    assert _pyproject()["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_no_capability_logic_yet():
    """CP1 is scaffolding. `SentienceGovernor` arrives in CP2, and this test
    is expected to be replaced then."""
    assert not hasattr(pydantic_ai_governor, "SentienceGovernor")
