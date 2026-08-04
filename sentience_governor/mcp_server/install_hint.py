"""Install guidance for the optional ``mcp`` extra, shared by the server entry
point and the CLI.

Deliberately free of heavy imports: the MCP server's failure path must be able
to explain itself *without* the MCP SDK present, and `cli/ux.py` must be able to
print the same guidance without importing the server module.

Two failure states must never be conflated (v0.3.0.1 §2.1):

* **absent** - the ``mcp`` package is not installed at all.
* **incompatible** - ``mcp`` *is* installed, but at a version this server does
  not support. v0.3.0 shipped an unbounded ``mcp>=1.0``; MCP SDK 2.0.0 removed
  ``mcp.server.fastmcp``, so every new install resolved a version that cannot
  run the server. Reporting that as "dependency missing" sent users to
  reinstall a package they already had.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

#: The range declared in ``pyproject.toml`` for the ``[mcp]`` extra. Kept here
#: so the runtime message and the packaging metadata cannot drift apart
#: silently; a mismatch is a test failure, not a surprise in the field.
MCP_SUPPORTED_RANGE = ">=1.0,<2"

_EXTRA_SPEC = 'sentience-governor[mcp]'

#: pipx writes this file into every venv it manages. Verified empirically
#: (2026-08-03) as the reliable marker: it is independent of where PIPX_HOME
#: points, unlike matching on a path fragment.
_PIPX_MARKER = "pipx_metadata.json"


def installed_mcp_version() -> Optional[str]:
    """The installed ``mcp`` distribution version, or None if absent.

    Never raises: this runs on a failure path, and an exception here would
    replace a useful diagnostic with a traceback.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("mcp")
    except Exception:  # PackageNotFoundError, or anything else
        return None


def detect_install_context() -> str:
    """Return ``"pipx"``, ``"venv"`` or ``"unknown"``.

    ``"unknown"`` is not a failure. It means the interpreter is neither a pipx
    venv nor any virtualenv, so how the user installed us cannot be determined
    (``pip install --user``, a system package, something else). Callers print
    both remediations in that case rather than guessing one and being wrong.
    """
    try:
        if (Path(sys.prefix) / _PIPX_MARKER).is_file():
            return "pipx"
        if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
            return "venv"
    except Exception:
        pass
    return "unknown"


def remediation_lines(*, repair: bool, context: Optional[str] = None) -> List[str]:
    """The exact command(s) to fix this install, for the detected context.

    ``repair=True`` means an unsupported ``mcp`` is already present, which needs
    a command that *re-resolves* rather than one that reports "already
    satisfied" and changes nothing. That distinction is the whole point: the
    defect being fixed here is a remediation that could not work.

    Commands are the ones verified in a clean isolated environment
    (v0.3.0.1 §2.2). A pipx-managed install is never told to run ambient pip.
    """
    ctx = context or detect_install_context()

    pipx_cmd = f'pipx install --force "{_EXTRA_SPEC}"'
    pip_cmd = (
        f'pip install --upgrade "{_EXTRA_SPEC}"'
        if repair
        else f'pip install "{_EXTRA_SPEC}"'
    )

    if ctx == "pipx":
        return [pipx_cmd]
    if ctx == "venv":
        return [pip_cmd]

    # Undetermined: show both, labelled, so the user picks by how they
    # installed rather than by guessing which line applies.
    return [
        f"if you installed with pipx:  {pipx_cmd}",
        f"if you installed with pip:   {pip_cmd}",
    ]


def _with_remediation(header: List[str], *, repair: bool) -> str:
    lines = list(header)
    lines.append("")
    for cmd in remediation_lines(repair=repair):
        lines.append(f"  {cmd}")
    return "\n".join(lines)


def absent_message() -> str:
    """The ``mcp`` package is not installed."""
    return _with_remediation(
        [
            "The Sentience MCP server requires the optional 'mcp' dependency, "
            "which is not installed.",
            f"Supported versions: mcp {MCP_SUPPORTED_RANGE}.",
            "Install it with:",
        ],
        repair=False,
    )


def incompatible_message(found: Optional[str]) -> str:
    """``mcp`` is installed, but this server cannot run against it."""
    found_str = found or "unknown"
    return _with_remediation(
        [
            f"The Sentience MCP server does not support the installed 'mcp' "
            f"version {found_str}.",
            f"Supported versions: mcp {MCP_SUPPORTED_RANGE}.",
            "The installed version does not provide 'mcp.server.fastmcp', "
            "which this server requires.",
            "The package is present, so reinstalling the extra alone will not "
            "help. Re-resolve it with:",
        ],
        repair=True,
    )


def import_failure_message() -> str:
    """Pick the right message by asking whether ``mcp`` is installed at all.

    Called from the ``ImportError`` handler around
    ``from mcp.server.fastmcp import FastMCP``. That import fails for both
    states, so the handler cannot tell them apart from the exception alone.
    """
    found = installed_mcp_version()
    if found is None:
        # Distribution metadata absent. Confirm the package really is missing
        # rather than merely unmeasurable, so a metadata quirk is not reported
        # as an absent package.
        import importlib.util

        try:
            if importlib.util.find_spec("mcp") is not None:
                return incompatible_message(None)
        except Exception:
            pass
        return absent_message()
    return incompatible_message(found)
