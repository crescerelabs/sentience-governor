"""CP8 — the CI surface, and the tag namespace CP10 will claim.

Two things are pinned here.

**The dedicated job's reason to exist.** The root `testpaths = ["tests"]`
scopes pytest discovery to the core suite, so a green core `test` run says
nothing about this integration. That is easy to forget and expensive to
rediscover, so it is asserted rather than documented.

**The tag namespace.** Rev 9 moved the integration tag workflow to CP10,
where the release check it invokes actually exists, and left CP8 with the
*static* confirmation that the namespace is safe to claim. This file is
that confirmation, in a form that keeps holding after CP10 lands.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path
from typing import List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PROJECT_ROOT.parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: The pattern CP10 will trigger the integration release workflow on.
INTEGRATION_TAG_GLOB = "pydantic-ai-governor-v*"
#: Core's existing pattern, owned by `release-check.yml`. Not ours to change.
CORE_TAG_GLOB = "v*"

repo_only = pytest.mark.skipif(
    not WORKFLOWS.is_dir(),
    reason="repository-only: the workflow tree is not part of the wheel")


# ---------------------------------------------------------------------------
# Why the dedicated job exists
# ---------------------------------------------------------------------------

@repo_only
def test_root_test_config_does_not_discover_the_integration_suite():
    """The core suite cannot stand in as integration coverage.

    If this ever became false the dedicated CI job would be redundant —
    but until then, a green core run is not evidence about this package,
    and that distinction is the whole reason `integration-pydantic-ai.yml`
    exists.
    """
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        root = tomllib.load(fh)
    testpaths = root["tool"]["pytest"]["ini_options"]["testpaths"]
    assert testpaths == ["tests"], (
        "root discovery is scoped to the core suite; if this changed, "
        "revisit whether the integration job is still needed")


@repo_only
def test_a_dedicated_integration_workflow_exists_and_runs_only_our_tests():
    workflow = (WORKFLOWS / "integration-pydantic-ai.yml").read_text()
    assert "integrations/pydantic-ai-governor/tests" in workflow, (
        "the job must name our test path explicitly, or root discovery "
        "would pick up the core suite instead")


@repo_only
def test_the_integration_job_covers_every_supported_python():
    """The declared floor and the matrix must not drift apart."""
    workflow = (WORKFLOWS / "integration-pydantic-ai.yml").read_text()
    for version in ("3.10", "3.11", "3.12", "3.13"):
        assert f'"{version}"' in workflow, f"Python {version} is not in the matrix"


@repo_only
def test_the_integration_job_covers_both_ends_of_the_dependency_range():
    """A bound nothing installs is a bound nothing tests."""
    workflow = (WORKFLOWS / "integration-pydantic-ai.yml").read_text()
    assert "pydantic-ai-slim==2.37.0" in workflow, "the floor is not pinned"
    assert "pydantic-ai-slim>=2.37.0,<2.38" in workflow, (
        "the ceiling is not resolved from the declared range")


# ---------------------------------------------------------------------------
# The tag namespace CP10 will claim (Rev 9: static confirmation only)
# ---------------------------------------------------------------------------

def test_the_integration_tag_namespace_cannot_match_core():
    """Disjoint by construction: our prefix does not begin with `v`.

    Checked as globs rather than argued in prose, so a future rename that
    breaks the property fails here instead of firing core's release gates
    on an integration tag.
    """
    integration_tags = ["pydantic-ai-governor-v0.1.0",
                        "pydantic-ai-governor-v0.1.0rc1",
                        "pydantic-ai-governor-v1.2.3"]
    core_tags = ["v0.3.1.2", "v1.0.0", "v0.3.2rc1"]

    for tag in integration_tags:
        assert fnmatch.fnmatch(tag, INTEGRATION_TAG_GLOB)
        assert not fnmatch.fnmatch(tag, CORE_TAG_GLOB), (
            f"{tag} would fire core's release gates")

    for tag in core_tags:
        assert fnmatch.fnmatch(tag, CORE_TAG_GLOB)
        assert not fnmatch.fnmatch(tag, INTEGRATION_TAG_GLOB), (
            f"{tag} would fire the integration release workflow")


@repo_only
def test_release_check_is_still_the_only_tag_triggered_workflow():
    """CP10 adds the second one. Until then, this pins the finding.

    When CP10 lands, this test is what tells the implementer that the
    landscape changed, rather than the change passing unremarked.
    """
    tagged: List[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        # Cheap and deliberate: a `tags:` key under a `push:` trigger.
        if "tags:" in text:
            tagged.append(path.name)

    assert tagged == ["release-check.yml"], (
        f"tag-triggered workflows changed: {tagged}. If CP10 added the "
        f"integration workflow, update this test to expect both.")


@repo_only
def test_cp8_introduced_no_release_machinery():
    """Rev 9 forbids it here, including any inline substitute.

    A throwaway approximation of CP10's gates would read as coverage while
    proving a path that CP10 then deletes.
    """
    assert not (PROJECT_ROOT / "scripts" / "release_check.py").exists()
    assert not (PROJECT_ROOT / "CHANGELOG.md").exists()
    workflow = (WORKFLOWS / "integration-pydantic-ai.yml").read_text()
    assert "tags:" not in workflow, "CP8 fires no tag workflow"
    assert "twine" not in workflow, "release gating is CP10's, not CP8's"
