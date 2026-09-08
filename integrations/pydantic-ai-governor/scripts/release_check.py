#!/usr/bin/env python3
"""release_check.py — the release gates for `pydantic-ai-governor`.

**This distribution's own gate, not the core one.** The core
`scripts/release_check.py` reads a single version from the root
`pyproject.toml` and asserts entries in the core changelogs. It is core-only
by construction and stays unaware of a second distribution; this script is
the replacement rather than an extension of it.

Same discipline as core: **a release gate is FALSE until its check has run
green HERE.** Do not tick a gate from memory.

    python integrations/pydantic-ai-governor/scripts/release_check.py
    python .../release_check.py --no-build     # fast: version/changelog/tests
    python .../release_check.py --no-tests     # skip gate 1 (see below)

Exits non-zero on any failed gate.

    1  tests pass          -> the integration suite, by explicit path
    4  version readable    -> from the integration pyproject.toml (CP1's
                              single authority; no hand-maintained __version__)
    7  changelog entry     -> "[<version>]" heading in the integration
                              CHANGELOG.md
    8  changelog isolation -> the integration changelog names no core release,
                              and core's changelogs name no integration release
    9  dependency direction-> core metadata declares no Pydantic dependency
    13 dist rebuilt        -> python -m build, into this package's own dist/
    14 twine check         -> twine check on the built artifacts
    15 wheel hygiene       -> no tests/, __pycache__ or .pyc in the wheel
    16 LICENSE in wheel    -> dist-info/licenses/LICENSE present
    17 wheel is ours only  -> only pydantic_ai_governor, no core source
    18 metadata correct    -> name, licence expression and both dependency
                              bounds as published contracts
"""
from __future__ import annotations

import argparse
import glob
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent          # integrations/pydantic-ai-governor
REPO = PKG.parent.parent                              # repository root
PY = sys.executable

GREEN, RED, DIM, RST = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
_results: list[tuple] = []


def gate(num, name, ok, detail=""):
    _results.append((num, name, ok))
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] gate {num:>2} — {name}")
    if detail and not ok:
        for line in detail.splitlines():
            print(f"         {DIM}{line}{RST}")


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd or PKG, capture_output=True, text=True)


def read_version() -> str | None:
    """The single version authority: this package's own pyproject.toml."""
    m = re.search(r'^version\s*=\s*"([^"]+)"',
                  (PKG / "pyproject.toml").read_text(), re.M)
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true",
                    help="skip the build, twine and artifact gates")
    ap.add_argument("--no-tests", action="store_true",
                    help="skip gate 1. Exists so the suite can exercise this "
                         "script without recursion: gate 1 runs the suite, so "
                         "a test that invokes the full gate would re-enter it.")
    args = ap.parse_args()

    print(f"\n  pydantic-ai-governor release gates  {DIM}({PKG}){RST}\n")

    # ---- gate 4: version, from the one authority ----------------------
    version = read_version()
    gate(4, "version readable from the integration pyproject.toml",
         bool(version), "no `version = \"...\"` found")
    if not version:
        return 1
    print(f"         {DIM}version {version}{RST}")

    # ---- gate 7: changelog entry --------------------------------------
    changelog = PKG / "CHANGELOG.md"
    text = changelog.read_text() if changelog.exists() else ""
    gate(7, f"CHANGELOG.md has a [{version}] entry",
         f"[{version}]" in text,
         f"add a `## [{version}]` heading to {changelog.name}")

    # ---- gate 8: the two ledgers stay separate ------------------------
    # A core release leaking into this changelog, or this release leaking
    # into core's, is exactly the coupling §5 forbids.
    core_versions = re.findall(r"^## \[(\d+\.\d+[\d.]*)\]", 
                               (REPO / "CHANGELOG.md").read_text(), re.M)
    ours_in_core = []
    for core_file in ("CHANGELOG.md", "docs/changelog.md"):
        p = REPO / core_file
        if p.exists() and re.search(rf"\[{re.escape(version)}\][^\n]*pydantic",
                                    p.read_text(), re.I):
            ours_in_core.append(core_file)
    core_in_ours = [v for v in core_versions if f"[{v}]" in text]
    gate(8, "changelogs stay separate (no core release here, none of ours there)",
         not core_in_ours and not ours_in_core,
         f"core releases named here: {core_in_ours}\n"
         f"this release named in core changelogs: {ours_in_core}")

    # ---- gate 9: dependency direction never inverts --------------------
    core_meta = (REPO / "pyproject.toml").read_text()
    core_deps = re.search(r"^dependencies\s*=\s*\[(.*?)\]", core_meta,
                          re.M | re.S)
    core_dep_text = core_deps.group(1) if core_deps else ""
    gate(9, "core declares no Pydantic AI dependency",
         "pydantic-ai" not in core_dep_text.lower(),
         "core's dependencies must never name pydantic-ai:\n" + core_dep_text)

    # ---- gate 1: the integration suite ---------------------------------
    if args.no_tests:
        print(f"         {DIM}gate  1 skipped (--no-tests){RST}")
    else:
        r = run([PY, "-m", "pytest", "-q", str(PKG / "tests")], cwd=REPO)
        gate(1, "integration test suite passes", r.returncode == 0,
             (r.stdout or "")[-2000:])

    if args.no_build:
        return _summary()

    # ---- gate 13: build ------------------------------------------------
    dist = PKG / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    r = run([PY, "-m", "build", "--outdir", str(dist), str(PKG)], cwd=REPO)
    wheels = glob.glob(str(dist / "*.whl"))
    sdists = glob.glob(str(dist / "*.tar.gz"))
    gate(13, "wheel and sdist build", r.returncode == 0 and wheels and sdists,
         (r.stderr or "")[-2000:])
    if not (wheels and sdists):
        return _summary()
    wheel = wheels[0]

    # ---- gate 14: twine ------------------------------------------------
    r = run([PY, "-m", "twine", "check", *wheels, *sdists], cwd=REPO)
    gate(14, "twine check", r.returncode == 0,
         (r.stdout or "") + (r.stderr or ""))

    members = zipfile.ZipFile(wheel).namelist()

    # ---- gate 15: wheel hygiene ----------------------------------------
    junk = [m for m in members
            if m.startswith("tests/") or "/tests/" in m
            or "__pycache__" in m or m.endswith(".pyc")]
    gate(15, "wheel hygiene (no tests, no __pycache__, no .pyc)", not junk,
         "\n".join(junk[:20]))

    # ---- gate 16: LICENSE bundled --------------------------------------
    licences = [m for m in members if m.endswith("licenses/LICENSE")
                or m.endswith(".dist-info/LICENSE")]
    gate(16, "LICENSE present in the wheel", bool(licences),
         "no dist-info LICENSE found")

    # ---- gate 17: our package only, no core source ---------------------
    stray = [m for m in members
             if not (m.startswith("pydantic_ai_governor/")
                     or ".dist-info/" in m)]
    core_src = [m for m in members if m.startswith("sentience_governor/")]
    gate(17, "wheel contains only pydantic_ai_governor, no core source",
         not stray and not core_src,
         f"stray: {stray[:10]}\ncore source: {core_src[:10]}")

    # ---- gate 18: published metadata contracts -------------------------
    meta = next((m for m in members if m.endswith(".dist-info/METADATA")), None)
    md = zipfile.ZipFile(wheel).read(meta).decode() if meta else ""
    checks = {
        "Name: pydantic-ai-governor": "Name: pydantic-ai-governor" in md,
        f"Version: {version}": f"Version: {version}" in md,
        "License-Expression: Apache-2.0": "License-Expression: Apache-2.0" in md,
        "core bound": "sentience-governor" in md and "<0.3.2" in md,
        "pydantic bound": "pydantic-ai-slim" in md and "<2.38" in md,
        "no core source dependency inversion": "Requires-Dist: pydantic-ai-slim" in md,
    }
    gate(18, "wheel metadata carries the published contracts",
         all(checks.values()),
         "\n".join(f"{k}: {v}" for k, v in checks.items() if not v))

    return _summary()


def _summary() -> int:
    failed = [f"gate {n} ({name})" for n, name, ok in _results if not ok]
    print()
    if failed:
        print(f"  {RED}FAILED{RST}: " + ", ".join(failed) + "\n")
        return 1
    print(f"  {GREEN}All {len(_results)} gates passed.{RST}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
