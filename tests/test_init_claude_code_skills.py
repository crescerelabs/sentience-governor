"""v0.2.7 CP3 — `sentience init claude-code` skill-install tests.

Two layers:
  * D8 v2.1 manifest algorithm, unit-tested against `_install_skills`
    with a controlled 2-skill bundle (cases 1-8 from the locked plan).
  * D10 detection + flags (`--no-skills`/`--project`/`--force`),
    integration-tested through `run_init_claude_code` with an isolated
    HOME and a faked hook binary.
"""

from __future__ import annotations

import argparse
import json
import sys

import pytest

from sentience_governor.cli import ux


# ----------------------------------------------------------------------
# Layer 1 — D8 manifest algorithm (unit, controlled bundle)
# ----------------------------------------------------------------------

V1_ALPHA = "---\nname: alpha\ndisable-model-invocation: true\n---\n!`sentience status`\n"
V2_ALPHA = "---\nname: alpha\ndisable-model-invocation: true\n---\n!`sentience status --v2`\n"
BETA = "---\nname: beta\ndisable-model-invocation: true\n---\n!`sentience pulse --latest --no-prompt`\n"


@pytest.fixture
def bundle(monkeypatch):
    """A live, mutable 2-skill bundle so tests can simulate a release
    bumping bundled content between installs."""
    state = {"alpha": V1_ALPHA, "beta": BETA}
    monkeypatch.setattr(ux, "_bundled_skills", lambda: dict(state))
    return state


def _manifest(root):
    return json.loads((root / ux._SKILLS_MANIFEST_NAME).read_text())


def _skill(root, name):
    return (root / name / "SKILL.md").read_text()


def test_case1_first_install_writes_manifest(tmp_path, bundle):
    root = tmp_path / "skills"
    summary = ux._install_skills(root, force=False)
    assert sorted(summary["installed"]) == ["alpha", "beta"]
    assert (root / ux._SKILLS_MANIFEST_NAME).is_file()
    assert sorted(_manifest(root)) == ["alpha", "beta"]
    assert _skill(root, "alpha") == V1_ALPHA


def test_case2_rerun_identical_is_noop(tmp_path, bundle):
    root = tmp_path / "skills"
    ux._install_skills(root, force=False)
    summary = ux._install_skills(root, force=False)
    assert sorted(summary["current"]) == ["alpha", "beta"]
    assert summary["installed"] == [] and summary["updated"] == []


def test_case3_bundle_change_with_manifest_match_overwrites(tmp_path, bundle):
    root = tmp_path / "skills"
    ux._install_skills(root, force=False)        # installs V1
    bundle["alpha"] = V2_ALPHA                    # new release bumps alpha
    summary = ux._install_skills(root, force=False)
    assert summary["updated"] == ["alpha"]
    assert summary["current"] == ["beta"]
    assert _skill(root, "alpha") == V2_ALPHA
    assert _manifest(root)["alpha"]["installed_hash"] == ux._skill_hash(V2_ALPHA)


def test_case4_handedit_preserved_without_force(tmp_path, bundle):
    root = tmp_path / "skills"
    ux._install_skills(root, force=False)
    (root / "alpha" / "SKILL.md").write_text(V1_ALPHA + "# operator note\n")
    summary = ux._install_skills(root, force=False)
    assert summary["preserved"] == ["alpha"]
    assert "# operator note" in _skill(root, "alpha")


def test_case5_manifest_missing_but_matches_bundle_is_adopted(tmp_path, bundle):
    root = tmp_path / "skills"
    ux._install_skills(root, force=False)
    (root / ux._SKILLS_MANIFEST_NAME).unlink()   # lose the manifest
    summary = ux._install_skills(root, force=False)
    assert sorted(summary["current"]) == ["alpha", "beta"]
    assert sorted(_manifest(root)) == ["alpha", "beta"]   # re-adopted


def test_case6_manifest_missing_and_differs_preserved_as_unmanaged(tmp_path, bundle):
    root = tmp_path / "skills"
    ux._install_skills(root, force=False)
    (root / ux._SKILLS_MANIFEST_NAME).unlink()
    (root / "alpha" / "SKILL.md").write_text(V1_ALPHA + "# hand\n")
    summary = ux._install_skills(root, force=False)
    assert summary["preserved"] == ["alpha"]
    assert "# hand" in _skill(root, "alpha")
    assert summary["current"] == ["beta"]        # beta still matches bundle


def test_case7_project_root_keeps_its_own_manifest(tmp_path, bundle):
    personal = tmp_path / "home" / ".claude" / "skills"
    project = tmp_path / "proj" / ".claude" / "skills"
    ux._install_skills(personal, force=False)
    ux._install_skills(project, force=False)
    assert (personal / ux._SKILLS_MANIFEST_NAME).is_file()
    assert (project / ux._SKILLS_MANIFEST_NAME).is_file()
    assert not (personal / ux._SKILLS_MANIFEST_NAME).samefile(
        project / ux._SKILLS_MANIFEST_NAME
    )


def test_case8_force_overwrites_and_rerecords(tmp_path, bundle):
    root = tmp_path / "skills"
    ux._install_skills(root, force=False)
    (root / "alpha" / "SKILL.md").write_text(V1_ALPHA + "# hand\n")
    summary = ux._install_skills(root, force=True)
    assert "alpha" in summary["updated"]
    assert _skill(root, "alpha") == V1_ALPHA            # edit clobbered
    assert _manifest(root)["alpha"]["installed_hash"] == ux._skill_hash(V1_ALPHA)


# ----------------------------------------------------------------------
# Layer 2 — D10 detection + flags (integration via run_init_claude_code)
# ----------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated HOME + faked hook binary + silenced PATH probe."""
    home = tmp_path / "home"
    home.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # v0.3.0.3: init verifies the resolved binary before writing (A11), so
    # the faked hook must be a real executable file.
    fake_hook = tmp_path / "bin" / "sentience-claude-code-hook"
    fake_hook.parent.mkdir(parents=True, exist_ok=True)
    fake_hook.write_text("#!/bin/sh\nexit 0\n")
    fake_hook.chmod(0o755)
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: str(fake_hook))
    monkeypatch.setattr(ux, "_probe_sentience_on_path", lambda: True)
    return home, proj


def _run(proj, **kw):
    args = argparse.Namespace(
        path=str(proj), no_skills=False, project=False, force=False
    )
    for k, v in kw.items():
        setattr(args, k, v)
    return ux.run_init_claude_code(args)


def test_default_install_writes_skills_under_home(env, capsys):
    home, proj = env
    rc = _run(proj)
    assert rc == 0
    skills = home / ".claude" / "skills"
    # Path.home() drove resolution (cross-platform smoke).
    assert (skills / "sentience-pulse" / "SKILL.md").is_file()
    assert (skills / ux._SKILLS_MANIFEST_NAME).is_file()
    assert "Restart any open Claude Code session" in capsys.readouterr().out


def test_no_skills_wires_hooks_only(env, capsys):
    home, proj = env
    rc = _run(proj, no_skills=True)
    assert rc == 0
    assert not (home / ".claude" / "skills").exists()
    assert (proj / ".claude" / "settings.local.json").is_file()   # hooks still wired


def test_project_flag_lands_in_project_dir(env, capsys):
    home, proj = env
    rc = _run(proj, project=True)
    assert rc == 0
    assert (proj / ".claude" / "skills" / "sentience-pulse" / "SKILL.md").is_file()
    assert not (home / ".claude" / "skills").exists()       # personal untouched
    # Project skills dir was absent before → restart guidance (D10).
    assert "Restart any open Claude Code session" in capsys.readouterr().out


def test_preexisting_skills_dir_says_no_restart(env, capsys):
    home, proj = env
    (home / ".claude" / "skills").mkdir(parents=True)        # already a skills user
    rc = _run(proj)
    assert rc == 0
    assert "no restart needed" in capsys.readouterr().out


def test_path_probe_warns_but_does_not_fail(env, monkeypatch, capsys):
    home, proj = env
    monkeypatch.setattr(ux, "_probe_sentience_on_path", lambda: False)
    rc = _run(proj)
    assert rc == 0                                           # warn, never fail
    err = capsys.readouterr().err
    assert "not resolvable on your PATH" in err


def test_probe_returns_false_when_binary_absent(monkeypatch):
    monkeypatch.setenv("PATH", "")                           # nothing resolvable
    assert ux._probe_sentience_on_path() is False


def test_probe_returns_true_when_binary_resolvable(monkeypatch):
    """True-positive path (the case the v0.2.7 build false-warned on)."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/sentience")
    assert ux._probe_sentience_on_path() is True


def test_sentience_version_flag(monkeypatch, capsys):
    """`sentience --version` exits 0 and prints the version (v0.2.7.1).

    Regression guard: the D10 PATH probe and the §16 troubleshooting docs
    both assume this flag exists. It did not before v0.2.7.1."""
    monkeypatch.setattr(sys, "argv", ["sentience", "--version"])
    with pytest.raises(SystemExit) as exc:
        ux.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("sentience ")


def test_skill_failure_is_fail_open_hooks_survive(env, monkeypatch, capsys):
    home, proj = env

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(ux, "_install_skills", _boom)
    rc = _run(proj)
    assert rc == 0                                           # R5 / acceptance #9
    assert (proj / ".claude" / "settings.local.json").is_file()    # hooks intact
    assert "could not install skills" in capsys.readouterr().err
