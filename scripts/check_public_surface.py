#!/usr/bin/env python3
"""Public-surface guard: fail if private-only references reach the public tree.

This is the standing invariant. `release_check.py`'s gate-10 forbidden-surface
check is NARROWER by design: its DOC_GLOBS cover only README.md, userdocs/**,
docs/website/**, Makefile and scripts/*.py, it exempts CHANGELOG.md by name, and
it runs on tags only. It therefore never inspects sentience_governor/**, which is
the highest-risk surface. This script covers the whole tree, on every push and
pull request.

Enumeration has two modes because the sanitization workflow runs outside any git
repository before the first commit exists:

  --mode=tree  recursive traversal, skipping nothing. Used during local staging.
  --mode=git   `git ls-files`, so newly tracked files are covered by default.
  (default: auto -- git if the tree is a git repo, else tree)

Both modes must cover the same files for the same tree.

Exemptions are exactly TWO paths, named explicitly. Each necessarily contains
the literal patterns it detects, so a no-exemptions rule would make this script
fail on every run. Both are read by a human at review time as compensation.

A single line may be allowed with a trailing marker (see ALLOW_MARKER). The
script reports the total marker count so exemptions cannot accumulate silently.

Exit 0 clean, 1 on any hit.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Directories that exist only in the internal repository. A reference to any of
# them from the public tree is a dangling pointer at best and a disclosure at
# worst.
PRIVATE_DIRS = [
    "docs/strategy",
    "docs/internal",
    "docs/gtm",
    "docs/plans",
    "docs/molly-qa",
    "docs/debug",
    "docs/announcements",
]

PATTERNS = [(d, re.compile(re.escape(d))) for d in PRIVATE_DIRS]
PATTERNS.append(("server/", re.compile(r"(?<![\w./-])server/")))
# Personal home paths. Not email addresses: any generic email regex flags the
# required pyproject maintainer address and the SECURITY.md disclosure route,
# and "personal vs role address" is not mechanically decidable. Email review is
# manual.
PATTERNS.append(("/Users/", re.compile(r"/Users/")))

# Exactly two, by design. Both must contain these literals to function.
EXEMPT_PATHS = {
    "scripts/check_public_surface.py",
    "scripts/release_check_forbidden.txt",
}

ALLOW_MARKER = "public-surface-allow"

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                   ".gz", ".whl", ".pyc", ".so", ".dylib"}


def enumerate_tree() -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            p = Path(dirpath) / fn
            out.append(str(p.relative_to(ROOT)))
    return sorted(out)


def enumerate_git() -> list[str]:
    res = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return sorted(l for l in res.stdout.splitlines() if l)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["auto", "tree", "git"], default="auto")
    args = ap.parse_args()

    mode = args.mode
    if mode == "auto":
        mode = "git" if (ROOT / ".git").exists() else "tree"

    files = enumerate_git() if mode == "git" else enumerate_tree()

    hits: list[str] = []
    markers = 0

    for rel in files:
        if rel in EXEMPT_PATHS:
            continue
        path = ROOT / rel
        if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # Each line is evaluated independently. No heading or section state may
        # disable checking for subsequent lines.
        for lineno, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARKER in line:
                markers += 1
                continue
            for label, rx in PATTERNS:
                if rx.search(line):
                    hits.append(f"{rel}:{lineno}: {label}  |  {line.strip()[:100]}")

    print(f"public-surface guard: mode={mode}, {len(files)} files scanned, "
          f"{len(EXEMPT_PATHS)} path exemptions, {markers} inline marker(s)")

    if hits:
        print(f"\nFAIL — {len(hits)} forbidden reference(s):\n")
        for h in hits:
            print(f"  {h}")
        return 1

    print("PASS — no forbidden references in the public surface.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
