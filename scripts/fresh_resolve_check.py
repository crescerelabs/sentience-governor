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


#: The intended PUBLIC MCP tool surface of this release. An independent
#: release contract, declared here and nowhere else: it is deliberately NOT
#: derived from the server implementation under test, so a tool that
#: disappears, or one that appears without a release decision, fails the gate.
#: Compared as a SET: MCP does not guarantee tool ordering and Sentience does
#: not promise one. Update this only as part of a release that changes the
#: public surface, and record the change.
EXPECTED_MCP_TOOLS = frozenset({
    "sentience_declare_intent",
    "sentience_explain",
    "sentience_intent",
    "sentience_profile_view",
    "sentience_pulse",
    "sentience_scan",
    "sentience_session_status",
    "sentience_violations",
})

#: The `sentience_explain` response contract the round-trip verifies.
EXPECTED_EXPLAIN_METHODOLOGY_VERSION = 1


def validate_tool_surface(names, expected=EXPECTED_MCP_TOOLS) -> None:
    """Fail unless the returned tool names are exactly the expected set.

    Order-insensitive. Raises AssertionError naming every missing and every
    unexpected tool, so the gate output says what changed, not just that
    something did.
    """
    got = set(names)
    missing = sorted(set(expected) - got)
    unexpected = sorted(got - set(expected))
    problems = []
    if missing:
        problems.append(f"missing expected tool(s): {missing}")
    if unexpected:
        problems.append(f"unexpected public tool(s): {unexpected}")
    if problems:
        raise AssertionError(
            "MCP public tool surface does not match the release contract; "
            + "; ".join(problems)
            + f"; got {sorted(got)}"
        )


def validate_explain_response(text: str) -> None:
    """Fail unless the `sentience_explain` payload satisfies its contract."""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"sentience_explain did not return JSON: {exc}; got {text!r:.200}") from exc
    if not isinstance(payload, dict):
        raise AssertionError(f"sentience_explain payload is not an object: {text!r:.200}")
    got = payload.get("methodology_version")
    if got != EXPECTED_EXPLAIN_METHODOLOGY_VERSION:
        raise AssertionError(
            f"sentience_explain methodology_version == {got!r}, "
            f"expected {EXPECTED_EXPLAIN_METHODOLOGY_VERSION}"
        )


#: Runs INSIDE the fresh venv (`python -c`), where this script is not
#: installed: it loads the contract functions above from this file by path
#: (stdlib-only imports at module level), so the fresh environment checks the
#: same contract the tests pin, and the mcp client comes from the venv.
_MCP_ROUNDTRIP_CODE = r"""
import asyncio, importlib.util, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER, GATE = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("fresh_resolve_gate", GATE)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

async def main():
    async with stdio_client(StdioServerParameters(command=SERVER, args=[])) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            names = [t.name for t in (await s.list_tools()).tools]
            gate.validate_tool_surface(names)
            res = await s.call_tool("sentience_explain", {})
            text = "".join(getattr(c, "text", "") for c in res.content)
            gate.validate_explain_response(text)
    print("ROUNDTRIP_OK")

asyncio.run(main())
"""


def mcp_roundtrip(py: Path, server: Path, timeout: int = 180) -> Tuple[bool, str]:
    """Initialize `server` over stdio from `py`'s environment, validate the
    public tool surface against EXPECTED_MCP_TOOLS, call `sentience_explain`
    and validate its response contract."""
    r = subprocess.run([str(py), "-c", _MCP_ROUNDTRIP_CODE, str(server), str(Path(__file__).resolve())],
                       capture_output=True, text=True, timeout=timeout, env=_clean_env())
    out = (r.stdout + r.stderr)
    # Keep the assertion, if any, visible: the tail of an exception-group
    # traceback is not where the message lives.
    tail = out[-3000:]
    lines = [ln for ln in out.splitlines() if "AssertionError" in ln]
    if lines and lines[-1] not in tail:
        tail = lines[-1] + "\n" + tail
    return (r.returncode == 0 and "ROUNDTRIP_OK" in out), tail


def smoke_mcp(py: Path) -> Tuple[bool, str]:
    """stdio round-trip against the intended public tool surface.

    The check that would have caught the v0.3.0 defect (an import of `mcp`
    succeeds under 2.x; it is *serving* that fails) and the v0.3.2 finding (a
    count hard-coded at v0.3.0.1 went stale when v0.3.1.1 added a tool, and
    the gate reported a failure that was not a product failure).

    The server binary is resolved from **this venv's** `bin/`, never via
    `shutil.which`. A PATH lookup would find whatever `sentience-mcp-server` the
    developer happens to have installed globally, so the gate would test the
    wrong artifact and could pass while the built wheel is broken.
    """
    server = py.parent / "sentience-mcp-server"
    if not server.is_file():
        return False, f"{server} not present: the wheel did not install the console script"
    return mcp_roundtrip(py, server)


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
    ("mcp", "[mcp]", smoke_mcp, "stdio round-trip and public tool surface"),
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
