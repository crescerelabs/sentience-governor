#!/usr/bin/env python3
"""release_check.py — run the mechanically-checkable release gates.

The discipline this enforces: **a release gate is FALSE until its check has run
green HERE.** Don't tick the Airtable "Release Gates" table from memory or
partial evidence — run this, and paste the output.

    make release-check                       # full (rebuilds the wheel)
    python scripts/release_check.py --no-build   # fast: version/docs/tests only

Exits non-zero on any failed gate. Gate map (Airtable order -> check):

    1   tests pass               -> pytest
    4   pyproject version        -> readable + mentioned in CHANGELOG
    7   CHANGELOG entry          -> "[<version>]" heading present
    8   user changelog entry     -> "## <version>" heading in docs/changelog.md
    10  public docs accurate     -> forbidden-surface grep: a removed/renamed
                                    surface must NOT appear as ACTIVE in
                                    user-facing docs or build tooling
    13  dist rebuilt             -> python -m build
    14  twine check              -> twine check dist/*
    15  wheel hygiene            -> no tests/ __pycache__ *.pyc in the wheel
    16  LICENSE in wheel         -> dist-info/licenses/LICENSE present

The gate-10 patterns live in scripts/release_check_forbidden.txt — update that
file each release when a surface is removed or renamed.

Gates 4 and 7 read the ROOT CHANGELOG.md only. Gate 8 exists because that was
not enough: the user-facing docs/changelog.md fell two releases behind and ended
up describing a removed command as though it still shipped, with every gate
green. A surface nothing checks is a surface that rots.
"""
from __future__ import annotations

import argparse
import glob
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# ACTIVE user-facing surfaces scanned by the gate-10 forbidden grep. Scoped
# deliberately: docs/design/** is historical design material describing what
# was true when a change was made, so it is not swept for currency. Build
# tooling is included so a removed CLI can't linger in the Makefile/scripts.
DOC_GLOBS = ["README.md", "docs/*.md", "docs/integrations/**/*.md",
             "docs/guide/**/*.md", "Makefile", "scripts/*.py"]
# Exemptions are PATH-specific, not filename-wide. Both changelogs are exempt
# from THIS grep because a changelog legitimately records what a past release
# shipped: the v0.2.8 entry naming `sentience-sync` is accurate history, not a
# stale claim. What went wrong before was not the exemption but that nothing
# else checked the user-facing changelog at all — gate 8 now does, so an entry
# documenting a removal is required rather than hoped for.
#
# Naming exact paths, rather than matching any file called changelog.md, keeps
# a future docs/<something>/changelog.md from inheriting the exemption silently.
EXEMPT_PATHS = {"CHANGELOG.md", "docs/changelog.md",
                "scripts/release_check.py", "scripts/check_public_surface.py"}
# A line may mention a forbidden term if it is clearly removal/sunset context.
LINE_ALLOW = re.compile(r"sunset|removed|no longer|deprecat|former", re.I)

GREEN, RED, DIM, RST = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
_results: list[tuple] = []


def gate(num, name, ok, detail=""):
    _results.append((num, name, ok))
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] gate {num:>2} — {name}")
    if detail and not ok:
        for line in detail.splitlines():
            print(f"         {DIM}{line}{RST}")


def run(cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)


def read_version():
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    return m.group(1) if m else None


def forbidden_patterns():
    f = ROOT / "scripts" / "release_check_forbidden.txt"
    out = []
    if f.exists():
        for ln in f.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(ln)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-build", action="store_true", help="skip build/twine/wheel")
    ap.add_argument("--no-tests", action="store_true", help="skip pytest")
    args = ap.parse_args()

    version = read_version()
    print(f"release_check — sentience-governor {version}\n")

    changelog = (ROOT / "CHANGELOG.md").read_text()

    # gate 4 — version consistency
    #
    # The README deliberately does NOT assert the version. It is also the
    # PyPI long_description, so a hardcoded version there goes stale on the
    # package page the moment the next release ships, and PyPI already
    # renders the release history itself. CHANGELOG is the authority for
    # "this version is documented"; coupling a marketing surface to a
    # release gate only fought the copy.
    v_ok = bool(version) and f"[{version}]" in changelog
    gate(4, "version consistent (CHANGELOG mentions it)", v_ok,
         f"version={version!r}  in_changelog={f'[{version}]' in changelog}")

    # gate 7 — changelog entry
    gate(7, "CHANGELOG entry for this version", bool(version) and f"[{version}]" in changelog)

    # gate 8 — the USER-FACING changelog must cover this version too.
    #
    # Root CHANGELOG.md and docs/changelog.md drifted apart for two releases
    # because nothing checked the second one: gates 4 and 7 only ever read the
    # root file. The user-facing page ended up describing a removed command as
    # though it still shipped. This gate is what stops that recurring.
    user_changelog = ROOT / "docs" / "changelog.md"
    if user_changelog.exists():
        text = user_changelog.read_text()
        ok = bool(version) and re.search(rf"^##\s+{re.escape(version)}\b",
                                         text, re.M) is not None
        gate(8, "user-facing changelog entry for this version (docs/changelog.md)",
             ok, f"no '## {version}' heading in docs/changelog.md")
    else:
        gate(8, "user-facing changelog entry for this version (docs/changelog.md)",
             False, "docs/changelog.md is missing")

    # gate 10 — forbidden-surface grep
    pats = [re.compile(p) for p in forbidden_patterns()]
    hits = []
    if pats:
        seen = set()
        for g in DOC_GLOBS:
            for p in glob.glob(str(ROOT / g), recursive=True):
                f = Path(p)
                if str(f.relative_to(ROOT)) in EXEMPT_PATHS or f in seen:
                    continue
                seen.add(f)
                try:
                    lines = f.read_text().splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                heading = ""
                for i, line in enumerate(lines, 1):
                    if re.match(r"^#{1,6}\s", line):
                        heading = line  # a "## … — sunset" section may discuss it freely
                    if LINE_ALLOW.search(line) or LINE_ALLOW.search(heading):
                        continue
                    if any(c.search(line) for c in pats):
                        hits.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()[:90]}")
    shown = "\n".join(hits[:25]) + (f"\n... +{len(hits) - 25} more" if len(hits) > 25 else "")
    gate(10, "no removed/renamed surface presented as active (user-facing docs + tooling)",
         not hits, shown)

    # gate 1 — tests
    if args.no_tests:
        print(f"  {DIM}[skip] gate  1 — tests (--no-tests){RST}")
    else:
        r = run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"])
        tail = (r.stdout.strip().splitlines() or [r.stderr[-300:]])[-1]
        gate(1, "tests pass (pytest)", r.returncode == 0, tail)

    # gates 13/14/15/16 — build + twine + wheel hygiene
    if args.no_build:
        print(f"  {DIM}[skip] gates 13-16 — build/twine/wheel (--no-build){RST}")
    else:
        run(["rm", "-rf", "dist", "build"])
        r = run([PY, "-m", "build"])
        gate(13, "dist/ rebuilt (wheel + sdist)", r.returncode == 0, r.stderr[-300:])
        if r.returncode == 0:
            dist = glob.glob(str(ROOT / "dist" / "*"))
            r = run([PY, "-m", "twine", "check", *dist])
            gate(14, "twine check", r.returncode == 0, r.stdout[-300:])
            whl = glob.glob(str(ROOT / "dist" / "*.whl"))
            if whl:
                names = zipfile.ZipFile(whl[0]).namelist()
                bad = [n for n in names
                       if n.startswith("tests/") or "__pycache__" in n or n.endswith(".pyc")]
                gate(15, "wheel hygiene (no tests/__pycache__/.pyc)", not bad, "\n".join(bad[:10]))
                gate(16, "LICENSE present in wheel",
                     any("licenses/LICENSE" in n or n.endswith("/LICENSE") for n in names))

    failed = [r for r in _results if not r[2]]
    print()
    if failed:
        print(f"{RED}release_check: {len(failed)} gate(s) FAILED{RST} — "
              f"fix before ticking the Release Gates table.")
        sys.exit(1)
    print(f"{GREEN}release_check: all checked gates PASSED.{RST} "
          f"Cite this output when ticking the gates.")


if __name__ == "__main__":
    main()
