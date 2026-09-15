#!/usr/bin/env python3
"""Decide whether the Pydantic AI integration matrix applies to this tree.

The companion distribution `pydantic-ai-governor` publishes a deliberate,
narrow dependency range on `sentience-governor` (its pyproject.toml). When
the core version in this tree lies OUTSIDE that range, installing the two
together is not a valid test of the candidate: pip satisfies the
companion's pin by pulling a published core over the editable one and the
suite runs in a mixed environment. In that case the integration matrix is
NOT APPLICABLE and says so; the companion's own release widens the range
when it has verified the new core (its compatibility proof lives there).

When the core version lies INSIDE the declared range, the matrix applies
exactly as before and every install and test step runs; failures fail CI.

Exit codes:
  0  a decision was reached (see `applicable=` in the output)
  2  the metadata could not be read or parsed; the caller must FAIL, never
     silently skip

Outputs (stdout, and appended to $GITHUB_OUTPUT when set):
  applicable=true|false
  core_version=<version>
  companion_version=<version>
  companion_requirement=<specifier>
  message=<one-line explanation>

Nothing here is hard-coded to a particular release: applicability is
derived from the two pyproject files as they are.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CORE_PYPROJECT = ROOT / "pyproject.toml"
COMPANION_PYPROJECT = ROOT / "integrations" / "pydantic-ai-governor" / "pyproject.toml"
CORE_DIST = "sentience-governor"
COMPANION_DIST = "pydantic-ai-governor"


class MetadataError(Exception):
    """Unreadable or malformed metadata. Callers must fail, not skip."""


def _load_toml(path: Path) -> dict:
    try:
        text = path.read_bytes()
    except OSError as exc:
        raise MetadataError(f"cannot read {path}: {exc}") from exc
    parser = None
    try:
        import tomllib as parser  # Python 3.11+
    except ModuleNotFoundError:
        try:
            import tomli as parser  # type: ignore[import-not-found,no-redef]
        except ModuleNotFoundError:
            parser = None
    if parser is None:
        return _minimal_toml(text.decode("utf-8"), path)
    try:
        return parser.loads(text.decode("utf-8"))
    except Exception as exc:  # TOMLDecodeError (either parser) or bad encoding
        raise MetadataError(f"{path}: not valid TOML: {exc}") from exc


def _minimal_toml(text: str, path: Path) -> dict:
    """Strict fallback for interpreters without a TOML parser: reads only
    `[project] version = "..."` and the `dependencies = [ ... ]` list of
    quoted strings. Anything it cannot find is a MetadataError."""
    version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    deps_block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.M | re.S)
    project: dict = {}
    if version:
        project["version"] = version.group(1)
    if deps_block:
        project["dependencies"] = re.findall(r'"([^"]+)"', deps_block.group(1))
    if not project:
        raise MetadataError(f"{path}: no parsable [project] fields (no TOML parser available)")
    return {"project": project}


def read_version(pyproject: Path) -> str:
    data = _load_toml(pyproject)
    version = (data.get("project") or {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise MetadataError(f"{pyproject}: [project].version missing or not a string")
    return version.strip()


def read_core_requirement(companion_pyproject: Path) -> str:
    """The companion's declared requirement on core, e.g. '>=0.3.1.2,<0.3.2'."""
    data = _load_toml(companion_pyproject)
    deps = (data.get("project") or {}).get("dependencies")
    if not isinstance(deps, list):
        raise MetadataError(f"{companion_pyproject}: [project].dependencies missing or not a list")
    matches: List[str] = []
    for dep in deps:
        if not isinstance(dep, str):
            raise MetadataError(f"{companion_pyproject}: dependency entry is not a string: {dep!r}")
        name, _, spec = _split_requirement(dep)
        if name.lower().replace("_", "-") == CORE_DIST:
            matches.append(spec)
    if len(matches) != 1:
        raise MetadataError(
            f"{companion_pyproject}: expected exactly one '{CORE_DIST}' dependency, found {len(matches)}"
        )
    spec = matches[0]
    if not spec:
        raise MetadataError(f"{companion_pyproject}: '{CORE_DIST}' dependency declares no version range")
    return spec


def _split_requirement(dep: str) -> Tuple[str, str, str]:
    """('name', extras, specifier) for a PEP 508 string without markers/URLs."""
    body = dep.split(";", 1)[0].strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", body)
    if not m:
        raise MetadataError(f"unparsable dependency string: {dep!r}")
    return m.group(1), m.group(2) or "", m.group(3).strip()


def decide(core_version: str, requirement: str) -> bool:
    """True iff `core_version` satisfies `requirement`. Raises MetadataError
    on an unparsable version or specifier."""
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ModuleNotFoundError as exc:  # pragma: no cover - CI installs packaging
        raise MetadataError("the 'packaging' library is required to evaluate the range") from exc
    try:
        spec = SpecifierSet(requirement)
    except InvalidSpecifier as exc:
        raise MetadataError(f"unparsable specifier {requirement!r}: {exc}") from exc
    try:
        version = Version(core_version)
    except InvalidVersion as exc:
        raise MetadataError(f"unparsable core version {core_version!r}: {exc}") from exc
    # prereleases=True so a pre-release core inside the range is applicable
    # rather than silently excluded by PEP 440's default handling.
    return spec.contains(version, prereleases=True)


def evaluate(
    core_pyproject: Path = CORE_PYPROJECT,
    companion_pyproject: Path = COMPANION_PYPROJECT,
    core_version_override: Optional[str] = None,
) -> dict:
    core_version = core_version_override or read_version(core_pyproject)
    companion_version = read_version(companion_pyproject)
    requirement = read_core_requirement(companion_pyproject)
    applicable = decide(core_version, requirement)
    if applicable:
        message = (
            f"{COMPANION_DIST} {companion_version} declares {CORE_DIST} {requirement}; "
            f"branch core is {core_version}; integration matrix applies."
        )
    else:
        message = (
            f"{COMPANION_DIST} {companion_version} declares {CORE_DIST} {requirement}; "
            f"branch core is {core_version}; integration matrix is not applicable to this "
            f"declared package pair."
        )
    return {
        "applicable": applicable,
        "core_version": core_version,
        "companion_version": companion_version,
        "companion_requirement": requirement,
        "message": message,
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--core-pyproject", type=Path, default=CORE_PYPROJECT)
    ap.add_argument("--companion-pyproject", type=Path, default=COMPANION_PYPROJECT)
    ap.add_argument("--core-version", default=None, help="override the core version (testing)")
    args = ap.parse_args(argv)
    try:
        result = evaluate(args.core_pyproject, args.companion_pyproject, args.core_version)
    except MetadataError as exc:
        print(f"integration_applicability: METADATA ERROR: {exc}", file=sys.stderr)
        return 2
    lines = [
        f"applicable={'true' if result['applicable'] else 'false'}",
        f"core_version={result['core_version']}",
        f"companion_version={result['companion_version']}",
        f"companion_requirement={result['companion_requirement']}",
        f"message={result['message']}",
    ]
    for line in lines:
        print(line)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
