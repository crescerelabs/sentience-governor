"""v0.3.0 CP7: `sentience init claude-code --mcp` — opt-in MCP registration.

Covers: no registration by default, opt-in registration into a project
.mcp.json, idempotence, preservation of other servers, the §5.1 consent
notice, and fail-open behavior when the server binary is missing or the
.mcp.json is unusable.
"""

import argparse
import json
from pathlib import Path

import pytest

from sentience_governor.cli import ux

FAKE_HOOK = "/fake/bin/sentience-claude-code-hook"
FAKE_MCP = "/fake/bin/sentience-mcp-server"


@pytest.fixture
def patched_binaries(monkeypatch):
    monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: FAKE_HOOK)
    monkeypatch.setattr(ux, "_resolve_mcp_server_binary", lambda: FAKE_MCP)


def _run(path: Path, *, mcp: bool = False) -> int:
    args = argparse.Namespace(
        path=str(path), no_skills=True, project=False, force=False, mcp=mcp
    )
    return ux.run_init_claude_code(args)


def _mcp_json(project: Path) -> Path:
    return project / ".mcp.json"


def _read_mcp(project: Path) -> dict:
    return json.loads(_mcp_json(project).read_text())


class TestNoDefaultRegistration:
    def test_without_mcp_flag_no_mcp_json_is_written(
        self, tmp_path, patched_binaries
    ):
        rc = _run(tmp_path, mcp=False)
        assert rc == 0
        # Hook is wired...
        assert (tmp_path / ".claude" / "settings.json").is_file()
        # ...but the MCP server is NOT registered by default.
        assert not _mcp_json(tmp_path).exists()

    def test_missing_mcp_attr_defaults_to_no_registration(
        self, tmp_path, patched_binaries
    ):
        # Older callers that never set `mcp` must not register anything.
        args = argparse.Namespace(
            path=str(tmp_path), no_skills=True, project=False, force=False
        )
        assert ux.run_init_claude_code(args) == 0
        assert not _mcp_json(tmp_path).exists()


class TestOptInRegistration:
    def test_mcp_flag_registers_server(self, tmp_path, patched_binaries, capsys):
        rc = _run(tmp_path, mcp=True)
        assert rc == 0
        config = _read_mcp(tmp_path)
        assert config["mcpServers"]["sentience"] == {
            "command": FAKE_MCP,
            "args": [],
        }
        out = capsys.readouterr().out
        assert "Sentience MCP server registered (opt-in)" in out

    def test_registration_is_idempotent(self, tmp_path, patched_binaries, capsys):
        _run(tmp_path, mcp=True)
        capsys.readouterr()
        _run(tmp_path, mcp=True)
        out = capsys.readouterr().out
        config = _read_mcp(tmp_path)
        # Exactly one sentience entry; second run reports it already present.
        assert list(config["mcpServers"]).count("sentience") == 1
        assert "already registered" in out

    def test_preserves_other_mcp_servers(self, tmp_path, patched_binaries):
        _mcp_json(tmp_path).write_text(
            json.dumps(
                {"mcpServers": {"other": {"command": "/usr/bin/other-mcp"}}},
                indent=2,
            )
        )
        _run(tmp_path, mcp=True)
        servers = _read_mcp(tmp_path)["mcpServers"]
        assert servers["other"] == {"command": "/usr/bin/other-mcp"}
        assert servers["sentience"]["command"] == FAKE_MCP

    def test_preserves_unrelated_top_level_keys(self, tmp_path, patched_binaries):
        _mcp_json(tmp_path).write_text(
            json.dumps({"someKey": True, "mcpServers": {}}, indent=2)
        )
        _run(tmp_path, mcp=True)
        config = _read_mcp(tmp_path)
        assert config["someKey"] is True
        assert config["mcpServers"]["sentience"]["command"] == FAKE_MCP


class TestConsentNotice:
    def test_notice_states_the_five_one_facts(
        self, tmp_path, patched_binaries, capsys
    ):
        _run(tmp_path, mcp=True)
        out = capsys.readouterr().out
        assert "read-only" in out
        assert "declare_intent appends" in out
        assert "append-only" in out
        assert "No policy or profile mutation tools" in out
        assert "No HTTP server is enabled (stdio only" in out
        assert "unavailable until a session ends (SessionEnd)" in out

    def test_notice_has_no_em_dashes(self, tmp_path, patched_binaries, capsys):
        _run(tmp_path, mcp=True)
        out = capsys.readouterr().out
        assert "—" not in out  # operator copy convention

    def test_notice_flags_missing_extra(
        self, tmp_path, patched_binaries, monkeypatch, capsys
    ):
        monkeypatch.setattr(ux, "_mcp_extra_installed", lambda: False)
        _run(tmp_path, mcp=True)
        out = capsys.readouterr().out
        assert 'pip install "sentience-governor[mcp]"' in out


class TestFailOpen:
    def test_missing_binary_warns_and_skips_without_failing(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setattr(ux, "_resolve_hook_binary", lambda: FAKE_HOOK)
        monkeypatch.setattr(ux, "_resolve_mcp_server_binary", lambda: None)
        rc = _run(tmp_path, mcp=True)
        assert rc == 0  # hooks still wired; init does not fail
        assert not _mcp_json(tmp_path).exists()
        err = capsys.readouterr().err
        assert "could not locate the 'sentience-mcp-server' binary" in err

    def test_non_object_mcp_json_is_not_clobbered(
        self, tmp_path, patched_binaries, capsys
    ):
        _mcp_json(tmp_path).write_text(json.dumps(["not", "an", "object"]))
        rc = _run(tmp_path, mcp=True)
        assert rc == 0
        # Original content is preserved (not overwritten).
        assert json.loads(_mcp_json(tmp_path).read_text()) == ["not", "an", "object"]
        assert "does not contain a JSON object" in capsys.readouterr().err
