#!/usr/bin/env python3
"""Standing release gate: unpinned fresh-environment resolve.

    make fresh-resolve                  # build a wheel from this tree, then gate it
    python scripts/fresh_resolve_check.py --wheel path/to.whl

**Why this exists.** v0.3.0 passed every release gate while shipping a broken
`[mcp]` extra. The gates ran in a developer environment whose `mcp` predated
2.0, so nothing ever resolved the dependency the way a new user's machine does.
The extra declared an unbounded `mcp>=1.0`; MCP SDK 2.0.0 removed
`mcp.server.fastmcp`, and every fresh install got a server that could not start.

The v0.3.0.1 bound fixes that instance. **This gate fixes the class**: any
future upstream major release that breaks a declared extra is caught here
instead of by a user.

**What makes it meaningful:**

* genuinely fresh venvs, one per extra, never reused
* dependencies resolve **only** from what the package declares. No tester pins,
  no constraints file, no `--no-deps`, and `PIP_*` environment overrides are
  stripped so an ambient constraint cannot silently rescue a broken declaration
* a **feature** smoke test per extra, not an import. An import succeeding while
  the server exits before serving is precisely the v0.3.0 failure
* every resolved version recorded from `pip --report`, an authoritative source
* **exit non-zero on failure.** A gate that warns is not a gate

Network-dependent, so this is its own target rather than part of the fast
offline `make release-check`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent

GREEN, RED, DIM, RST = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

#: Environment variables that could smuggle a pin or an alternate index into
#: what is supposed to be a declaration-only resolve. Cleared for every child.
_PIP_OVERRIDES = (
    "PIP_CONSTRAINT",
    "PIP_REQUIRE_HASHES",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
    "PIP_NO_INDEX",
    "PIP_CONFIG_FILE",
    "PIP_UPGRADE",
    "PIP_PRE",
)

_results: List[Tuple[str, bool, str]] = []


def _clean_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _PIP_OVERRIDES}
    # A tester's ~/.sentience must not leak into a "fresh" environment.
    env["SENTIENCE_NO_FIRST_RUN_PROMPT"] = "1"
    return env


def record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    tag = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{tag}] {name}")
    if detail and not ok:
        for line in detail.strip().splitlines()[:12]:
            print(f"         {DIM}{line}{RST}")


def build_wheel(outdir: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir), str(ROOT)],
        check=True, capture_output=True, env=_clean_env(),
    )
    wheels = sorted(outdir.glob("*.whl"))
    if not wheels:
        raise SystemExit("build produced no wheel")
    return wheels[-1]


def make_env(where: Path) -> Path:
    """A genuinely fresh venv. No system site packages, never reused."""
    if where.exists():
        shutil.rmtree(where)
    venv.EnvBuilder(with_pip=True, clear=True, symlinks=True).create(where)
    return where / "bin" / "python"


def install(py: Path, spec: str, report: Path) -> Tuple[bool, str]:
    r = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check",
         "--report", str(report), spec],
        capture_output=True, text=True, env=_clean_env(),
    )
    return r.returncode == 0, (r.stdout + r.stderr)[-4000:]


def resolved(report: Path) -> Dict[str, str]:
    """Resolved versions, from pip's own report. Never from an importlib probe
    inside the target environment: that can read a leaked outer installation."""
    if not report.is_file():
        return {}
    data = json.loads(report.read_text())
    return {
        i["metadata"]["name"].lower().replace("_", "-"): i["metadata"]["version"]
        for i in data.get("install", [])
    }


def run(py: Path, code: str, timeout: int = 120) -> Tuple[bool, str]:
    r = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                       timeout=timeout, env=_clean_env())
    return r.returncode == 0, (r.stdout + r.stderr)[-3000:]


# --------------------------------------------------------------------------
# Feature smoke tests. Each exercises the entry point its extra exists for.
# --------------------------------------------------------------------------

def smoke_base(py: Path) -> Tuple[bool, str]:
    for args in (["--version"], ["explain"]):
        exe = py.parent / "sentience"
        r = subprocess.run([str(exe), *args], capture_output=True, text=True,
                           timeout=120, env=_clean_env())
        if r.returncode != 0:
            return False, f"sentience {' '.join(args)} -> exit {r.returncode}\n{r.stderr[-800:]}"
    return True, ""


def smoke_mcp(py: Path) -> Tuple[bool, str]:
    """stdio round-trip listing all seven tools.

    The check that would have caught the v0.3.0 defect. An import of `mcp`
    succeeds under 2.x; it is *serving* that fails.

    The server binary is resolved from **this venv's** `bin/`, never via
    `shutil.which`. A PATH lookup would find whatever `sentience-mcp-server` the
    developer happens to have installed globally, so the gate would test the
    wrong artifact and could pass while the built wheel is broken.
    """
    server = py.parent / "sentience-mcp-server"
    if not server.is_file():
        return False, f"{server} not present: the wheel did not install the console script"
    code = r"""
import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = 7
SERVER = sys.argv[1] if len(sys.argv) > 1 else None

async def main():
    server = SERVER
    assert server, "server path not supplied"
    async with stdio_client(StdioServerParameters(command=server, args=[])) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            assert len(tools) == EXPECTED, f"expected {EXPECTED} tools, got {len(tools)}: {tools}"
            res = await s.call_tool("sentience_explain", {})
            text = "".join(getattr(c, "text", "") for c in res.content)
            assert json.loads(text).get("methodology_version") == 1, "explain round-trip failed"
    print("ROUNDTRIP_OK")

asyncio.run(main())
"""
    r = subprocess.run([str(py), "-c", code, str(server)], capture_output=True,
                       text=True, timeout=180, env=_clean_env())
    out = (r.stdout + r.stderr)[-3000:]
    return (r.returncode == 0 and "ROUNDTRIP_OK" in out), out


def smoke_dev(py: Path) -> Tuple[bool, str]:
    r = subprocess.run(
        [str(py), "-m", "pytest", "--collect-only", "-q", str(ROOT / "tests")],
        capture_output=True, text=True, timeout=300, env=_clean_env(),
    )
    return r.returncode == 0, (r.stdout + r.stderr)[-1500:]


def smoke_demo(py: Path) -> Tuple[bool, str]:
    """Resolve and import only.

    The demos need live API keys, so a functional test is not gate-able
    offline. **This limit is stated in the gate output** rather than left to be
    inferred from a green tick.
    """
    ok, out = run(py, "import anthropic, pyairtable, langchain_core; print('IMPORTS_OK')")
    return (ok and "IMPORTS_OK" in out), out


EXTRAS = [
    ("base", "", smoke_base, "sentience --version, sentience explain"),
    ("mcp", "[mcp]", smoke_mcp, "stdio round-trip listing 7 tools"),
    ("dev", "[dev]", smoke_dev, "collect the suite"),
    ("demo", "[demo]", smoke_demo, "RESOLVE AND IMPORT ONLY (see note)"),
]


def gate(wheel: Path, workdir: Path, only: Optional[str] = None) -> bool:
    print(f"fresh-resolve gate — {wheel.name}\n")
    all_versions: Dict[str, Dict[str, str]] = {}

    for name, suffix, smoke, desc in EXTRAS:
        if only and name != only:
            continue
        env_dir = workdir / f"venv-{name}"
        report = workdir / f"report-{name}.json"
        py = make_env(env_dir)

        ok, out = install(py, f"{wheel}{suffix}", report)
        if not ok:
            record(f"{name:<5} install (unpinned, declaration-only)", False, out)
            continue

        versions = resolved(report)
        all_versions[name] = versions
        record(f"{name:<5} install (unpinned, declaration-only)", True)

        ok, out = smoke(py)
        record(f"{name:<5} smoke: {desc}", ok, out)

    print("\n  resolved dependency versions (from pip --report):")
    for name, versions in all_versions.items():
        head = ", ".join(f"{k}=={v}" for k, v in sorted(versions.items())[:6])
        print(f"    {name:<5} {len(versions)} packages   {head}{' …' if len(versions) > 6 else ''}")
        for path in (workdir / f"report-{name}.json",):
            if path.is_file():
                print(f"          full report: {path}")

    print(f"\n  {DIM}NOTE: the [demo] extra is resolve-and-import only. Its demos need"
          f" live API keys,{RST}")
    print(f"  {DIM}      so a functional test is not gate-able offline. This gate does"
          f" NOT prove{RST}")
    print(f"  {DIM}      the demos run.{RST}")

    failed = [n for n, ok, _ in _results if not ok]
    print()
    if failed:
        print(f"{RED}fresh-resolve: {len(failed)} check(s) FAILED{RST} — the release is blocked.")
        return False
    print(f"{GREEN}fresh-resolve: all checks PASSED.{RST} Cite this output when ticking the gates.")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wheel", help="gate this wheel instead of building from the tree")
    ap.add_argument("--only", choices=[e[0] for e in EXTRAS], help="run one extra only")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = ap.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="fresh-resolve-"))
    try:
        wheel = Path(args.wheel).resolve() if args.wheel else build_wheel(workdir / "dist")
        return 0 if gate(wheel, workdir, args.only) else 1
    finally:
        if args.keep:
            print(f"\n  scratch kept at {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
