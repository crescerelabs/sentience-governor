"""The version-aware applicability guard for the Pydantic AI integration matrix.

`scripts/integration_applicability.py` derives, from the two pyproject
files, whether the companion's declared `sentience-governor` range admits
the core version in this tree. Inside the range the integration matrix
runs exactly as before; outside it the matrix is reported not applicable
instead of installing an incompatible pair. Malformed or unreadable
metadata must fail (exit 2), never silently skip.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "integration_applicability.py"


def _load():
    spec = importlib.util.spec_from_file_location("integration_applicability", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


mod = _load()


def _companion(tmp_path: Path, deps, version="0.1.0") -> Path:
    body = "\n".join(f'    "{d}",' for d in deps)
    p = tmp_path / "companion-pyproject.toml"
    p.write_text(f'[project]\nname = "pydantic-ai-governor"\nversion = "{version}"\ndependencies = [\n{body}\n]\n', encoding="utf-8")
    return p


def _core(tmp_path: Path, version: str) -> Path:
    p = tmp_path / "core-pyproject.toml"
    p.write_text(f'[project]\nname = "sentience-governor"\nversion = "{version}"\n', encoding="utf-8")
    return p


REAL_COMPANION = ROOT / "integrations" / "pydantic-ai-governor" / "pyproject.toml"
REAL_CORE = ROOT / "pyproject.toml"


class TestRangeLogic:
    @pytest.mark.parametrize("core,expected", [
        ("0.3.1.2", True), ("0.3.1.3", True), ("0.3.1.9", True),
        ("0.3.2", False), ("0.3.2.1", False), ("0.3.3", False), ("0.3.1.1", False), ("0.4", False),
    ])
    def test_against_the_companion_0_1_0_range(self, core, expected):
        assert mod.decide(core, ">=0.3.1.2,<0.3.2") is expected

    def test_against_the_planned_0_1_1_range(self):
        assert mod.decide("0.3.2", ">=0.3.2,<0.3.3") is True
        assert mod.decide("0.3.2.1", ">=0.3.2,<0.3.3") is True
        assert mod.decide("0.3.3", ">=0.3.2,<0.3.3") is False
        assert mod.decide("0.3.1.2", ">=0.3.2,<0.3.3") is False

    def test_prerelease_inside_range_is_applicable(self):
        assert mod.decide("0.3.1.3rc1", ">=0.3.1.2,<0.3.2") is True

    def test_unparsable_inputs_raise(self):
        with pytest.raises(mod.MetadataError):
            mod.decide("not-a-version", ">=0.3.1.2,<0.3.2")
        with pytest.raises(mod.MetadataError):
            mod.decide("0.3.2", ">=banana")


class TestMetadataReading:
    def test_reads_the_real_companion_requirement(self):
        req = mod.read_core_requirement(REAL_COMPANION)
        assert req == ">=0.3.1.2,<0.3.2"  # the published 0.1.0 pin, unchanged by CP8

    def test_current_tree_is_not_applicable_until_the_companion_widens(self):
        result = mod.evaluate(REAL_CORE, REAL_COMPANION)
        assert result["core_version"] == mod.read_version(REAL_CORE)
        assert result["applicable"] is False
        assert "pydantic-ai-governor 0.1.0 declares sentience-governor >=0.3.1.2,<0.3.2" in result["message"]
        assert f"branch core is {result['core_version']}" in result["message"]
        assert "not applicable to this declared package pair" in result["message"]

    def test_core_inside_range_is_applicable(self, tmp_path):
        result = mod.evaluate(_core(tmp_path, "0.3.1.2"), REAL_COMPANION)
        assert result["applicable"] is True and "integration matrix applies" in result["message"]
        result = mod.evaluate(REAL_CORE, REAL_COMPANION, core_version_override="0.3.1.5")
        assert result["applicable"] is True

    def test_widened_companion_makes_current_core_applicable(self, tmp_path):
        companion = _companion(tmp_path, ["sentience-governor>=0.3.2,<0.3.3", "pydantic-ai-slim>=2.37.0,<2.38"], version="0.1.1")
        result = mod.evaluate(_core(tmp_path, "0.3.2"), companion)
        assert result["applicable"] is True
        assert result["companion_version"] == "0.1.1"

    def test_extras_and_markers_do_not_confuse_the_parser(self, tmp_path):
        companion = tmp_path / "companion-pyproject.toml"
        companion.write_text(
            "[project]\nname = \"pydantic-ai-governor\"\nversion = \"0.1.0\"\n"
            "dependencies = [\n    'sentience-governor[mcp]>=0.3.1.2,<0.3.2 ; python_version >= \"3.10\"',\n]\n",
            encoding="utf-8",
        )
        assert mod.read_core_requirement(companion) == ">=0.3.1.2,<0.3.2"

    def test_invalid_toml_is_a_metadata_error(self, tmp_path):
        bad = tmp_path / "broken.toml"
        bad.write_text('[project]\nversion = "0.1.0"\ndependencies = [\n', encoding="utf-8")
        with pytest.raises(mod.MetadataError):
            mod.evaluate(REAL_CORE, bad)

    @pytest.mark.parametrize("deps", [
        [],                                                  # no core dependency
        ["pydantic-ai-slim>=2.37.0,<2.38"],                  # core absent
        ["sentience-governor"],                              # no range
        ["sentience-governor>=0.3.1.2,<0.3.2", "sentience-governor<1"],  # two entries
        ["sentience-governor>=nope"],                        # unparsable specifier
    ])
    def test_malformed_dependency_metadata_fails(self, tmp_path, deps):
        companion = _companion(tmp_path, deps)
        with pytest.raises(mod.MetadataError):
            mod.evaluate(_core(tmp_path, "0.3.2"), companion)

    def test_missing_files_and_missing_version_fail(self, tmp_path):
        with pytest.raises(mod.MetadataError):
            mod.evaluate(tmp_path / "absent.toml", REAL_COMPANION)
        with pytest.raises(mod.MetadataError):
            mod.evaluate(REAL_CORE, tmp_path / "absent.toml")
        bad = tmp_path / "noversion.toml"
        bad.write_text('[project]\nname = "sentience-governor"\n', encoding="utf-8")
        with pytest.raises(mod.MetadataError):
            mod.evaluate(bad, REAL_COMPANION)

    def test_minimal_parser_fallback_reads_the_same_fields(self):
        text = REAL_COMPANION.read_text(encoding="utf-8")
        data = mod._minimal_toml(text, REAL_COMPANION)
        assert data["project"]["version"] == "0.1.0"
        assert "sentience-governor>=0.3.1.2,<0.3.2" in data["project"]["dependencies"]
        with pytest.raises(mod.MetadataError):
            mod._minimal_toml("nothing here", REAL_COMPANION)


class TestCommandLine:
    def _run(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def test_not_applicable_exit_zero_with_message(self, tmp_path):
        out = tmp_path / "gh_output"
        import os
        env = dict(os.environ, GITHUB_OUTPUT=str(out))
        r = self._run("--core-version", "0.3.2", env=env)
        assert r.returncode == 0, r.stderr
        assert "applicable=false" in r.stdout
        assert "not applicable to this declared package pair" in r.stdout
        assert "applicable=false" in out.read_text(encoding="utf-8")

    def test_applicable_exit_zero(self):
        r = self._run("--core-version", "0.3.1.2")
        assert r.returncode == 0 and "applicable=true" in r.stdout

    def test_malformed_metadata_exits_two(self, tmp_path):
        companion = _companion(tmp_path, ["pydantic-ai-slim>=2.37.0,<2.38"])
        r = self._run("--companion-pyproject", str(companion))
        assert r.returncode == 2
        assert "METADATA ERROR" in r.stderr
        assert "applicable=" not in r.stdout
