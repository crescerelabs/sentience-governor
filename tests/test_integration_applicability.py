"""The two-mode guard for the Pydantic AI integration matrix.

`scripts/integration_applicability.py` derives, from the two pyproject
files, whether the companion's declared `sentience-governor` range admits
the core version in this tree (declared package compatibility), and reports
a MODE: `declared-compatible` (install the pair through normal dependency
resolution and run the suite) or `backward-compat-probe` (install the
branch core explicitly and the companion without its core pin, assert the
candidate is what is installed, run the complete suite). The decision is
never "run versus skip". Malformed or unreadable metadata must fail
(exit 2), never silently skip.
"""

from __future__ import annotations

import importlib.util
import os
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

REAL_COMPANION = ROOT / "integrations" / "pydantic-ai-governor" / "pyproject.toml"
REAL_CORE = ROOT / "pyproject.toml"


def _companion(tmp_path: Path, deps, version="0.1.0", dev=None) -> Path:
    body = "\n".join(f'    "{d}",' for d in deps)
    text = f'[project]\nname = "pydantic-ai-governor"\nversion = "{version}"\ndependencies = [\n{body}\n]\n'
    if dev is not None:
        text += "\n[project.optional-dependencies]\ndev = [\n" + "\n".join(f'    "{d}",' for d in dev) + "\n]\n"
    p = tmp_path / "companion-pyproject.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _core(tmp_path: Path, version: str) -> Path:
    p = tmp_path / "core-pyproject.toml"
    p.write_text(f'[project]\nname = "sentience-governor"\nversion = "{version}"\n', encoding="utf-8")
    return p


def _real_companion_metadata():
    """The companion's version and core requirement read straight from its
    pyproject with the stdlib parser: the oracle for every real-pair test, so
    the suite never hard-codes what the companion currently declares."""
    import tomllib
    data = tomllib.loads(REAL_COMPANION.read_text(encoding="utf-8"))["project"]
    core = [d for d in data["dependencies"] if d.lower().startswith("sentience-governor")]
    assert len(core) == 1
    return data["version"], core[0][len("sentience-governor"):].split(";")[0].strip(), data


# The 0.1.0-shaped declaration (`<0.3.2`) is kept as a FIXTURE: it is the
# published metadata that first exercised Mode B against core 0.3.2, and the
# probe path must stay covered whatever the real companion declares today.
LEGACY_RANGE = ">=0.3.1.2,<0.3.2"


class TestModes:
    def test_real_pair_mode_is_derived_from_the_real_metadata(self, tmp_path):
        """For any core version, the real companion's mode is exactly what its
        own declared range says: no literal pin of the current release."""
        version, requirement, _ = _real_companion_metadata()
        for v in ("0.3.1.2", "0.3.1.9", "0.3.2", "0.3.2.1", "0.3.3", "0.4"):
            r = mod.evaluate(_core(tmp_path, v), REAL_COMPANION)
            expected = mod.decide(v, requirement)
            assert r["declared_compatible"] is expected, v
            assert r["mode"] == (mod.MODE_DECLARED if expected else mod.MODE_PROBE), v
            assert r["companion_version"] == version and r["companion_requirement"] == requirement
            assert f"pydantic-ai-governor {version} declares sentience-governor {requirement}" in r["message"]
            assert f"branch core is {v}" in r["message"]
            assert "not applicable" not in r["message"].lower()

    def test_mode_a_declared_compatible_fixture(self, tmp_path):
        companion = _companion(tmp_path, [f"sentience-governor{LEGACY_RANGE}", "pydantic-ai-slim>=2.37.0,<2.38"])
        for v in ("0.3.1.2", "0.3.1.3", "0.3.1.9"):
            r = mod.evaluate(_core(tmp_path, v), companion)
            assert r["mode"] == mod.MODE_DECLARED and r["declared_compatible"] is True, v
            assert "declared package compatibility: YES" in r["message"]

    def test_mode_b_backward_compat_probe_fixture(self, tmp_path):
        """The exact situation of core 0.3.2 against published companion 0.1.0."""
        companion = _companion(tmp_path, [f"sentience-governor{LEGACY_RANGE}", "pydantic-ai-slim>=2.37.0,<2.38"])
        r = mod.evaluate(_core(tmp_path, "0.3.2"), companion)
        assert r["mode"] == mod.MODE_PROBE and r["declared_compatible"] is False
        assert f"pydantic-ai-governor 0.1.0 declares sentience-governor {LEGACY_RANGE}" in r["message"]
        assert "branch core is 0.3.2" in r["message"]
        assert "declared package compatibility: NO" in r["message"]
        assert "behavioral backward-compatibility probe" in r["message"]
        assert "not applicable" not in r["message"].lower()

    def test_current_tree_mode_matches_the_real_core_version(self):
        r = mod.evaluate(REAL_CORE, REAL_COMPANION)
        expected = mod.MODE_DECLARED if mod.decide(r["core_version"], r["companion_requirement"]) else mod.MODE_PROBE
        assert r["mode"] == expected

    def test_future_companion_range_with_core_0_3_2_is_declared_compatible(self, tmp_path):
        companion = _companion(tmp_path, ["sentience-governor>=0.3.2,<0.3.3", "pydantic-ai-slim>=2.37.0,<2.38"], version="0.1.1")
        r = mod.evaluate(_core(tmp_path, "0.3.2"), companion)
        assert r["mode"] == mod.MODE_DECLARED and r["companion_version"] == "0.1.1"
        r = mod.evaluate(_core(tmp_path, "0.3.3"), companion)
        assert r["mode"] == mod.MODE_PROBE

    def test_the_decision_is_a_mode_not_run_versus_skip(self):
        assert {mod.MODE_DECLARED, mod.MODE_PROBE} == {"declared-compatible", "backward-compat-probe"}
        assert "skip" not in mod.MODE_PROBE and "applicable" not in mod.MODE_PROBE


class TestRangeLogic:
    @pytest.mark.parametrize("core,expected", [
        ("0.3.1.2", True), ("0.3.1.3", True), ("0.3.1.9", True),
        ("0.3.2", False), ("0.3.2.1", False), ("0.3.3", False), ("0.3.1.1", False), ("0.4", False),
    ])
    def test_against_the_companion_0_1_0_range(self, core, expected):
        assert mod.decide(core, ">=0.3.1.2,<0.3.2") is expected

    def test_prerelease_inside_range_counts(self):
        assert mod.decide("0.3.1.3rc1", ">=0.3.1.2,<0.3.2") is True

    def test_unparsable_inputs_raise(self):
        with pytest.raises(mod.MetadataError):
            mod.decide("not-a-version", ">=0.3.1.2,<0.3.2")
        with pytest.raises(mod.MetadataError):
            mod.decide("0.3.2", ">=banana")


class TestMetadataReading:
    def test_reads_the_real_companion_requirement(self):
        _, requirement, _ = _real_companion_metadata()
        got = mod.read_core_requirement(REAL_COMPANION)
        assert got == requirement
        # A deliberate, bounded range: a floor and a ceiling (0.1.0 plan §16).
        assert ">=" in got and "<" in got

    def test_non_core_requirements_from_the_real_companion(self):
        reqs = mod.non_core_requirements(REAL_COMPANION)
        assert not any(r.lower().startswith("sentience-governor") for r in reqs)
        assert any(r.startswith("pydantic-ai-slim") for r in reqs)
        assert any(r.startswith("pytest") for r in reqs) and any(r.startswith("anyio") for r in reqs)
        marker = [r for r in reqs if r.startswith("tomli")]
        assert marker and "python_version < '3.11'" in marker[0]  # marker preserved verbatim

    def test_extras_and_markers_do_not_confuse_the_parser(self, tmp_path):
        companion = tmp_path / "companion-pyproject.toml"
        companion.write_text(
            "[project]\nname = \"pydantic-ai-governor\"\nversion = \"0.1.0\"\n"
            "dependencies = [\n    'sentience-governor[mcp]>=0.3.1.2,<0.3.2 ; python_version >= \"3.10\"',\n"
            "    'pydantic-ai-slim>=2.37.0,<2.38',\n]\n",
            encoding="utf-8",
        )
        assert mod.read_core_requirement(companion) == ">=0.3.1.2,<0.3.2"
        assert mod.non_core_requirements(companion) == ["pydantic-ai-slim>=2.37.0,<2.38"]

    @pytest.mark.parametrize("deps", [
        [],                                                  # no core dependency
        ["pydantic-ai-slim>=2.37.0,<2.38"],                  # core absent
        ["sentience-governor"],                              # no range
        ["sentience-governor>=0.3.1.2,<0.3.2", "sentience-governor<1"],  # duplicate
        ["sentience-governor>=nope"],                        # unparsable specifier
    ])
    def test_malformed_or_missing_core_declaration_fails_hard(self, tmp_path, deps):
        companion = _companion(tmp_path, deps)
        with pytest.raises(mod.MetadataError):
            mod.evaluate(_core(tmp_path, "0.3.2"), companion)

    def test_invalid_toml_and_missing_files_fail_hard(self, tmp_path):
        bad = tmp_path / "broken.toml"
        bad.write_text('[project]\nversion = "0.1.0"\ndependencies = [\n', encoding="utf-8")
        with pytest.raises(mod.MetadataError):
            mod.evaluate(REAL_CORE, bad)
        with pytest.raises(mod.MetadataError):
            mod.evaluate(tmp_path / "absent.toml", REAL_COMPANION)
        with pytest.raises(mod.MetadataError):
            mod.evaluate(REAL_CORE, tmp_path / "absent.toml")
        noversion = tmp_path / "noversion.toml"
        noversion.write_text('[project]\nname = "sentience-governor"\n', encoding="utf-8")
        with pytest.raises(mod.MetadataError):
            mod.evaluate(noversion, REAL_COMPANION)

    def test_no_non_core_requirements_is_a_metadata_error(self, tmp_path):
        companion = _companion(tmp_path, ["sentience-governor>=0.3.1.2,<0.3.2"])
        with pytest.raises(mod.MetadataError):
            mod.non_core_requirements(companion)

    def test_minimal_parser_fallback_reads_the_same_fields(self):
        """The no-TOML-parser fallback must agree with the real parser on the
        real file, whatever the companion currently declares."""
        version, _, project = _real_companion_metadata()
        text = REAL_COMPANION.read_text(encoding="utf-8")
        data = mod._minimal_toml(text, REAL_COMPANION)
        assert data["project"]["version"] == version
        assert data["project"]["dependencies"] == project["dependencies"]
        assert data["project"]["optional-dependencies"]["dev"] == project["optional-dependencies"]["dev"]
        assert any(d.startswith("pytest") for d in data["project"]["optional-dependencies"]["dev"])
        with pytest.raises(mod.MetadataError):
            mod._minimal_toml("nothing here", REAL_COMPANION)


class TestCommandLine:
    def _run(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def test_probe_mode_exit_zero_with_outputs_and_requirements_file(self, tmp_path):
        """Mode B on the command line, against the 0.1.0-shaped fixture."""
        companion = _companion(tmp_path, [f"sentience-governor{LEGACY_RANGE}", "pydantic-ai-slim>=2.37.0,<2.38"],
                               dev=["pytest>=7.0", "tomli>=2.0; python_version < '3.11'"])
        out = tmp_path / "gh_output"
        reqs = tmp_path / "probe-requirements.txt"
        env = dict(os.environ, GITHUB_OUTPUT=str(out))
        r = self._run("--companion-pyproject", str(companion), "--core-version", "0.3.2",
                      "--write-probe-requirements", str(reqs), env=env)
        assert r.returncode == 0, r.stderr
        assert "mode=backward-compat-probe" in r.stdout and "declared_compatible=false" in r.stdout
        assert "declared package compatibility: NO" in r.stdout
        gh = out.read_text(encoding="utf-8")
        assert "mode=backward-compat-probe" in gh and "core_version=0.3.2" in gh
        lines = reqs.read_text(encoding="utf-8").splitlines()
        assert lines and not any(l.startswith("sentience-governor") for l in lines)
        assert any(l.startswith("pydantic-ai-slim") for l in lines)
        assert "tomli>=2.0; python_version < '3.11'" in lines  # marker preserved

    def test_declared_mode_exit_zero(self, tmp_path):
        """Mode A on the command line, against a fixture inside its own range."""
        companion = _companion(tmp_path, [f"sentience-governor{LEGACY_RANGE}", "pydantic-ai-slim>=2.37.0,<2.38"])
        r = self._run("--companion-pyproject", str(companion), "--core-version", "0.3.1.2")
        assert r.returncode == 0 and "mode=declared-compatible" in r.stdout and "declared_compatible=true" in r.stdout

    def test_real_pair_on_the_command_line_matches_evaluate(self):
        """The real repository pair, exit 0, reporting whatever its metadata
        implies (declared-compatible once the companion admits this core)."""
        r = self._run()
        assert r.returncode == 0, r.stderr
        expected = mod.evaluate(REAL_CORE, REAL_COMPANION)
        assert f"mode={expected['mode']}" in r.stdout
        assert f"declared_compatible={'true' if expected['declared_compatible'] else 'false'}" in r.stdout
        assert f"companion_version={expected['companion_version']}" in r.stdout

    def test_malformed_metadata_exits_two(self, tmp_path):
        companion = _companion(tmp_path, ["pydantic-ai-slim>=2.37.0,<2.38"])
        r = self._run("--companion-pyproject", str(companion))
        assert r.returncode == 2
        assert "METADATA ERROR" in r.stderr
        assert "mode=" not in r.stdout
