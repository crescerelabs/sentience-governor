#!/usr/bin/env python3
"""Decide HOW the Pydantic AI integration matrix runs against this tree.

Two compatibility concepts, kept distinct:

* **Declared package compatibility.** The companion distribution
  `pydantic-ai-governor` publishes a deliberate, narrow dependency range on
  `sentience-governor` (its pyproject.toml). The core version in this tree
  either lies inside that range (the pair is officially installable through
  normal dependency resolution) or outside it (normal pip co-installation is
  unsupported until the companion widens its range).

* **Behavioral backward compatibility.** Whether the companion's own test
  suite still passes against the core candidate in this tree, regardless of
  what the companion's metadata declares.

The published range decides the first; it never decides the second. So the
helper reports a MODE rather than "run versus skip":

  mode=declared-compatible     core inside the declared range: install the
                               pair through normal dependency resolution and
                               run the companion suite; any failure fails CI.
  mode=backward-compat-probe   core outside the declared range: install the
                               branch core explicitly, install the companion
                               WITHOUT its dependency declaration (so pip
                               cannot replace or downgrade the candidate),
                               install the companion's non-core requirements
                               explicitly, assert the environment holds the
                               candidate, then run the complete companion
                               suite; any failure fails CI.

Exit codes:
  0  a decision was reached (see `mode=` in the output)
  2  the metadata could not be read or parsed; the caller must FAIL, never
     silently skip

Outputs (stdout, and appended to $GITHUB_OUTPUT when set):
  mode=declared-compatible|backward-compat-probe
  declared_compatible=true|false
  core_version=<version>
  companion_version=<version>
  companion_requirement=<specifier>
  message=<one-line explanation>

`--write-probe-requirements PATH` additionally writes the companion's
declared requirements EXCEPT its `sentience-governor` line (runtime
dependencies plus the `dev` extra, markers preserved) as a pip requirements
file, so the probe installs exactly what the companion declares, minus the
core pin that would fight the candidate.

Nothing here is hard-coded to a particular release: everything is derived
from the two pyproject files as they are.
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

MODE_DECLARED = "declared-compatible"
MODE_PROBE = "backward-compat-probe"


class MetadataError(Exception):
    """Unreadable or malformed metadata. Callers must fail, not skip."""


# ---------------------------------------------------------------------------
# metadata reading
# ---------------------------------------------------------------------------

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
    `[project] version = "..."`, the `dependencies = [ ... ]` list and the
    `[project.optional-dependencies] dev = [ ... ]` list of quoted strings.
    Anything it cannot find is a MetadataError."""
    version = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    deps_block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.M | re.S)
    dev_block = re.search(r"^dev\s*=\s*\[(.*?)\]", text, re.M | re.S)
    project: dict = {}
    if version:
        project["version"] = version.group(1)
    if deps_block:
        project["dependencies"] = re.findall(r'"([^"]+)"', deps_block.group(1))
    if dev_block:
        project["optional-dependencies"] = {"dev": re.findall(r'"([^"]+)"', dev_block.group(1))}
    if not project:
        raise MetadataError(f"{path}: no parsable [project] fields (no TOML parser available)")
    return {"project": project}


def read_version(pyproject: Path) -> str:
    data = _load_toml(pyproject)
    version = (data.get("project") or {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise MetadataError(f"{pyproject}: [project].version missing or not a string")
    return version.strip()


def _dependencies(companion_pyproject: Path) -> Tuple[List[str], List[str]]:
    """(runtime dependencies, dev extra) as declared, validated as string lists."""
    data = _load_toml(companion_pyproject)
    project = data.get("project") or {}
    deps = project.get("dependencies")
    if not isinstance(deps, list):
        raise MetadataError(f"{companion_pyproject}: [project].dependencies missing or not a list")
    for dep in deps:
        if not isinstance(dep, str):
            raise MetadataError(f"{companion_pyproject}: dependency entry is not a string: {dep!r}")
    dev = (project.get("optional-dependencies") or {}).get("dev") or []
    if not isinstance(dev, list) or any(not isinstance(d, str) for d in dev):
        raise MetadataError(f"{companion_pyproject}: [project.optional-dependencies].dev is not a list of strings")
    return list(deps), list(dev)


def read_core_requirement(companion_pyproject: Path) -> str:
    """The companion's declared requirement on core, e.g. '>=0.3.1.2,<0.3.2'."""
    deps, _ = _dependencies(companion_pyproject)
    matches: List[str] = []
    for dep in deps:
        name, _, spec = _split_requirement(dep)
        if _is_core(name):
            matches.append(spec)
    if len(matches) != 1:
        raise MetadataError(
            f"{companion_pyproject}: expected exactly one '{CORE_DIST}' dependency, found {len(matches)}"
        )
    spec = matches[0]
    if not spec:
        raise MetadataError(f"{companion_pyproject}: '{CORE_DIST}' dependency declares no version range")
    return spec


def non_core_requirements(companion_pyproject: Path) -> List[str]:
    """Every declared requirement except the core pin: runtime dependencies
    plus the `dev` extra, markers preserved, in declaration order."""
    deps, dev = _dependencies(companion_pyproject)
    out: List[str] = []
    for dep in deps + dev:
        name, _, _ = _split_requirement(dep)
        if not _is_core(name):
            out.append(dep.strip())
    if not out:
        raise MetadataError(f"{companion_pyproject}: no non-core requirements declared")
    return out


def _is_core(name: str) -> bool:
    return name.lower().replace("_", "-") == CORE_DIST


def _split_requirement(dep: str) -> Tuple[str, str, str]:
    """('name', extras, specifier) for a PEP 508 string; markers are dropped."""
    body = dep.split(";", 1)[0].strip()
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", body)
    if not m:
        raise MetadataError(f"unparsable dependency string: {dep!r}")
    return m.group(1), m.group(2) or "", m.group(3).strip()


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------

def decide(core_version: str, requirement: str) -> bool:
    """True iff `core_version` satisfies `requirement` (declared compatibility).
    Raises MetadataError on an unparsable version or specifier."""
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
    # prereleases=True so a pre-release core inside the range counts as
    # declared-compatible rather than being excluded by PEP 440's default.
    return spec.contains(version, prereleases=True)


def evaluate(
    core_pyproject: Path = CORE_PYPROJECT,
    companion_pyproject: Path = COMPANION_PYPROJECT,
    core_version_override: Optional[str] = None,
) -> dict:
    core_version = core_version_override or read_version(core_pyproject)
    companion_version = read_version(companion_pyproject)
    requirement = read_core_requirement(companion_pyproject)
    declared = decide(core_version, requirement)
    mode = MODE_DECLARED if declared else MODE_PROBE
    if declared:
        message = (
            f"{COMPANION_DIST} {companion_version} declares {CORE_DIST} {requirement}; "
            f"branch core is {core_version}; declared package compatibility: YES; "
            f"the pair installs through normal dependency resolution."
        )
    else:
        message = (
            f"{COMPANION_DIST} {companion_version} declares {CORE_DIST} {requirement}; "
            f"branch core is {core_version}; declared package compatibility: NO; "
            f"running the behavioral backward-compatibility probe against the branch candidate."
        )
    return {
        "mode": mode,
        "declared_compatible": declared,
        "core_version": core_version,
        "companion_version": companion_version,
        "companion_requirement": requirement,
        "message": message,
    }


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--core-pyproject", type=Path, default=CORE_PYPROJECT)
    ap.add_argument("--companion-pyproject", type=Path, default=COMPANION_PYPROJECT)
    ap.add_argument("--core-version", default=None, help="override the core version (testing)")
    ap.add_argument(
        "--write-probe-requirements", type=Path, default=None,
        help="write the companion's non-core requirements (runtime + dev, markers kept) to this file",
    )
    args = ap.parse_args(argv)
    try:
        result = evaluate(args.core_pyproject, args.companion_pyproject, args.core_version)
        if args.write_probe_requirements is not None:
            reqs = non_core_requirements(args.companion_pyproject)
            args.write_probe_requirements.write_text("\n".join(reqs) + "\n", encoding="utf-8")
    except MetadataError as exc:
        print(f"integration_applicability: METADATA ERROR: {exc}", file=sys.stderr)
        return 2
    lines = [
        f"mode={result['mode']}",
        f"declared_compatible={'true' if result['declared_compatible'] else 'false'}",
        f"core_version={result['core_version']}",
        f"companion_version={result['companion_version']}",
        f"companion_requirement={result['companion_requirement']}",
        f"message={result['message']}",
    ]
    for line in lines:
        print(line)
    if args.write_probe_requirements is not None:
        print(f"probe_requirements={args.write_probe_requirements}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
