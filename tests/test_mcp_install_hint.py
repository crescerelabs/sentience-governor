"""v0.3.0.1 CP0 — the MCP compatibility repair.

Locks in the six regression assertions the plan requires. Each exists because
the shipped v0.3.0 behaviour failed it:

1. the incompatible-version path does not claim the dependency is missing
2. the incompatible-version path does not exit 0
3. the absent-package path does not exit 0
4. the pipx branch prints the verified pipx remediation
5. the pipx branch never emits the bare ambient ``pip install`` string
6. an undetermined context prints both alternatives, labelled

Plus a drift guard: the runtime's supported range must match what
``pyproject.toml`` actually declares.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sentience_governor.mcp_server import install_hint

PIP_BARE = 'pip install "sentience-governor[mcp]"'
PIPX_CMD = 'pipx install --force "sentience-governor[mcp]"'


class TestFailureStatesAreDistinguished:
    """The v0.3.0 defect: both states produced the identical message."""

    def test_incompatible_does_not_claim_the_dependency_is_missing(self):
        msg = install_hint.incompatible_message("2.0.0")
        assert "2.0.0" in msg
        assert install_hint.MCP_SUPPORTED_RANGE in msg
        # The exact wrong turn v0.3.0 took.
        assert "not installed" not in msg
        assert "is missing" not in msg

    def test_absent_says_not_installed(self):
        msg = install_hint.absent_message()
        assert "not installed" in msg

    def test_the_two_messages_differ(self):
        assert install_hint.absent_message() != install_hint.incompatible_message("2.0.0")

    def test_incompatible_names_the_missing_module(self):
        # Without this the user cannot tell why a present package is unusable.
        assert "mcp.server.fastmcp" in install_hint.incompatible_message("2.0.0")


class TestRemediationIsContextAware:
    def test_pipx_context_prints_the_verified_pipx_command(self):
        assert install_hint.remediation_lines(repair=True, context="pipx") == [PIPX_CMD]

    def test_pipx_context_never_emits_bare_ambient_pip(self):
        for repair in (True, False):
            joined = " ".join(
                install_hint.remediation_lines(repair=repair, context="pipx")
            )
            assert PIP_BARE not in joined
            assert "pip install" not in joined.replace("pipx install", "")

    def test_venv_repair_uses_upgrade_not_a_no_op(self):
        # Plain `pip install` reports "already satisfied" and changes nothing
        # when the package is present, which is the failure mode being fixed.
        lines = install_hint.remediation_lines(repair=True, context="venv")
        assert lines == ['pip install --upgrade "sentience-governor[mcp]"']

    def test_venv_absent_uses_plain_install(self):
        assert install_hint.remediation_lines(repair=False, context="venv") == [PIP_BARE]

    def test_unknown_context_prints_both_labelled(self):
        lines = install_hint.remediation_lines(repair=True, context="unknown")
        assert len(lines) == 2
        joined = "\n".join(lines)
        assert "pipx" in joined and "pip" in joined
        # Labelled, so the user chooses by how they installed.
        assert all(line.startswith("if you installed with") for line in lines)


class TestDetectionMarker:
    def test_pipx_marker_is_detected(self, tmp_path, monkeypatch):
        (tmp_path / "pipx_metadata.json").write_text("{}")
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        assert install_hint.detect_install_context() == "pipx"

    def test_plain_venv_is_not_reported_as_pipx(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "other"))
        assert install_hint.detect_install_context() == "venv"

    def test_non_venv_is_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "prefix", str(tmp_path))
        monkeypatch.setattr(sys, "base_prefix", str(tmp_path))
        assert install_hint.detect_install_context() == "unknown"


class TestExitCodes:
    """Both paths already exit non-zero. Asserted so it cannot regress.

    Measured by subprocess with no shell pipe: reading an exit status through a
    pipe reports the exit code of the last command in the pipeline, which is
    how the original grounding pass got this wrong.
    """

    @pytest.mark.parametrize("simulate", ["absent", "incompatible"])
    def test_build_server_exits_non_zero(self, simulate, tmp_path):
        script = f"""
import sys, builtins
_real = builtins.__import__
def _blocked(name, *a, **k):
    if name == "mcp.server.fastmcp" or name.startswith("mcp.server"):
        raise ImportError("simulated")
    return _real(name, *a, **k)
builtins.__import__ = _blocked

from sentience_governor.mcp_server import install_hint
install_hint.installed_mcp_version = lambda: {"None" if simulate == "absent" else "'2.0.0'"}
import importlib.util
install_hint.importlib = importlib.util

from sentience_governor.mcp_server.server import build_server
build_server()
"""
        f = tmp_path / "probe.py"
        f.write_text(script)
        r = subprocess.run(
            [sys.executable, str(f)],
            capture_output=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert r.returncode != 0, "build_server must exit non-zero on import failure"


class TestSupportedRangeDoesNotDrift:
    def test_runtime_range_matches_pyproject(self):
        """A message advertising a range the package does not declare is worse
        than no message: it sends the user to install something unsupported."""
        root = Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text()
        assert f'"mcp{install_hint.MCP_SUPPORTED_RANGE}"' in text.replace(" ", "")

    def test_pyproject_bounds_mcp_below_2(self):
        root = Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text().replace(" ", "")
        assert '"mcp>=1.0,<2"' in text
