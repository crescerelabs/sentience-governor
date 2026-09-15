"""The `[mcp]` portion of the fresh-resolve release gate (`make fresh-resolve`).

At the v0.3.2 freeze the gate failed on a tool COUNT hard-coded at v0.3.0.1
(`EXPECTED = 7`) after v0.3.1.1 had added `sentience_scan`: a stale gate, not
a product defect, and one that had also gone unnoticed against the published
0.3.1.2. The gate now asserts the intended public tool surface as an
independently declared set, and these tests pin the gate itself so the
failure cannot silently recur or be silently weakened.

The contract set below is written out literally on purpose: it must never be
derived from the server implementation under test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fresh_resolve_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("fresh_resolve_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


gate = _load()

#: The v0.3.2 public MCP tool surface, restated independently of the script.
CONTRACT = {
    "sentience_declare_intent",
    "sentience_explain",
    "sentience_intent",
    "sentience_profile_view",
    "sentience_pulse",
    "sentience_scan",
    "sentience_session_status",
    "sentience_violations",
}


# ---------------------------------------------------------------------------
# the declared contract
# ---------------------------------------------------------------------------

def test_the_gate_declares_the_eight_tool_contract_literally():
    assert set(gate.EXPECTED_MCP_TOOLS) == CONTRACT
    assert len(gate.EXPECTED_MCP_TOOLS) == 8
    assert isinstance(gate.EXPECTED_MCP_TOOLS, frozenset)


def test_the_contract_is_not_read_from_the_server_implementation():
    source = SCRIPT.read_text(encoding="utf-8")
    # The gate must never import the server, nor consult its tool manager, to
    # learn what to expect: that would make the check circular.
    assert "sentience_governor.mcp_server" not in source
    assert "_tool_manager" not in source
    assert "build_server" not in source


def test_the_gate_label_describes_the_contract_not_a_count():
    labels = {name: desc for name, _, _, desc in gate.EXTRAS}
    assert labels["mcp"] == "stdio round-trip and public tool surface"
    assert not any(ch.isdigit() for ch in labels["mcp"])


# ---------------------------------------------------------------------------
# validate_tool_surface
# ---------------------------------------------------------------------------

def test_exact_surface_passes_in_any_order():
    names = sorted(CONTRACT)
    gate.validate_tool_surface(names)
    gate.validate_tool_surface(list(reversed(names)))
    gate.validate_tool_surface(tuple(names[3:] + names[:3]))
    gate.validate_tool_surface(iter(names))


def test_a_missing_expected_tool_fails_and_is_named():
    names = sorted(CONTRACT - {"sentience_scan"})
    with pytest.raises(AssertionError) as exc:
        gate.validate_tool_surface(names)
    assert "missing expected tool(s): ['sentience_scan']" in str(exc.value)
    assert "unexpected" not in str(exc.value)


def test_an_unexpected_additional_tool_fails_and_is_named():
    names = sorted(CONTRACT | {"sentience_delete_everything"})
    with pytest.raises(AssertionError) as exc:
        gate.validate_tool_surface(names)
    assert "unexpected public tool(s): ['sentience_delete_everything']" in str(exc.value)
    assert "missing" not in str(exc.value)


def test_a_renamed_tool_reports_both_missing_and_unexpected():
    names = sorted((CONTRACT - {"sentience_pulse"}) | {"sentience_pulse_v2"})
    with pytest.raises(AssertionError) as exc:
        gate.validate_tool_surface(names)
    msg = str(exc.value)
    assert "['sentience_pulse']" in msg and "['sentience_pulse_v2']" in msg


def test_the_seven_tool_surface_of_v0_3_0_1_fails_today():
    """The exact stale state that blocked the v0.3.2 freeze, inverted: a
    server WITHOUT `sentience_scan` must fail the current contract."""
    with pytest.raises(AssertionError):
        gate.validate_tool_surface(CONTRACT - {"sentience_scan"})


def test_the_expected_set_can_be_overridden_for_a_future_release():
    gate.validate_tool_surface(["a", "b"], expected={"a", "b"})
    with pytest.raises(AssertionError):
        gate.validate_tool_surface(["a"], expected={"a", "b"})


# ---------------------------------------------------------------------------
# validate_explain_response
# ---------------------------------------------------------------------------

def test_explain_contract_is_methodology_version_one():
    assert gate.EXPECTED_EXPLAIN_METHODOLOGY_VERSION == 1
    gate.validate_explain_response(json.dumps({"methodology_version": 1, "other": "x"}))


@pytest.mark.parametrize("text", [
    json.dumps({"methodology_version": 2}),
    json.dumps({"methodology_version": "1"}),
    json.dumps({}),
    json.dumps([1]),
    "not json",
    "",
])
def test_explain_contract_violations_fail(text):
    with pytest.raises(AssertionError):
        gate.validate_explain_response(text)


# ---------------------------------------------------------------------------
# the round-trip over real stdio, against fake servers with chosen surfaces
# ---------------------------------------------------------------------------

def _fake_server(tmp_path: Path, tools, explain_payload) -> Path:
    """An executable stdio MCP server exposing exactly `tools`, whose
    `sentience_explain` (if present) returns `explain_payload` as JSON."""
    pytest.importorskip("mcp")
    body = f'''#!{sys.executable}
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-sentience")
TOOLS = {list(tools)!r}
EXPLAIN = {json.dumps(explain_payload)!r}

def _make(name):
    def tool() -> str:
        return EXPLAIN if name == "sentience_explain" else json.dumps({{"tool": name}})
    tool.__name__ = name
    return tool

for _name in TOOLS:
    mcp.tool(name=_name)(_make(_name))

if __name__ == "__main__":
    mcp.run()
'''
    path = tmp_path / "fake-sentience-mcp-server"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


GOOD_EXPLAIN = {"methodology_version": 1, "verdict": "ok"}


def test_roundtrip_passes_against_the_exact_surface(tmp_path):
    server = _fake_server(tmp_path, sorted(CONTRACT), GOOD_EXPLAIN)
    ok, out = gate.mcp_roundtrip(Path(sys.executable), server)
    assert ok, out
    assert "ROUNDTRIP_OK" in out


def test_roundtrip_is_order_insensitive(tmp_path):
    names = sorted(CONTRACT)
    server = _fake_server(tmp_path, list(reversed(names)), GOOD_EXPLAIN)
    ok, out = gate.mcp_roundtrip(Path(sys.executable), server)
    assert ok, out


def test_roundtrip_fails_when_an_expected_tool_is_missing(tmp_path):
    server = _fake_server(tmp_path, sorted(CONTRACT - {"sentience_scan"}), GOOD_EXPLAIN)
    ok, out = gate.mcp_roundtrip(Path(sys.executable), server)
    assert not ok
    assert "missing expected tool(s): ['sentience_scan']" in out
    assert "ROUNDTRIP_OK" not in out


def test_roundtrip_fails_when_an_unexpected_tool_appears(tmp_path):
    server = _fake_server(tmp_path, sorted(CONTRACT | {"sentience_extra"}), GOOD_EXPLAIN)
    ok, out = gate.mcp_roundtrip(Path(sys.executable), server)
    assert not ok
    assert "unexpected public tool(s): ['sentience_extra']" in out


def test_roundtrip_fails_when_explain_breaks_its_contract(tmp_path):
    server = _fake_server(tmp_path, sorted(CONTRACT), {"methodology_version": 2})
    ok, out = gate.mcp_roundtrip(Path(sys.executable), server)
    assert not ok
    assert "methodology_version == 2, expected 1" in out


def test_roundtrip_fails_when_the_server_cannot_start(tmp_path):
    broken = tmp_path / "broken-server"
    broken.write_text(f"#!{sys.executable}\nraise SystemExit('boom')\n", encoding="utf-8")
    broken.chmod(broken.stat().st_mode | stat.S_IXUSR)
    ok, out = gate.mcp_roundtrip(Path(sys.executable), broken, timeout=60)
    assert not ok
    assert "ROUNDTRIP_OK" not in out


def test_smoke_mcp_requires_the_console_script_in_the_venv(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ok, out = gate.smoke_mcp(fake_bin / "python")
    assert not ok
    assert "did not install the console script" in out


def test_smoke_mcp_against_this_environments_real_server():
    """The real 0.3.2 server in this test environment must satisfy the
    contract end to end. Skipped only where the console script is not
    installed next to the interpreter."""
    pytest.importorskip("mcp")
    py = Path(sys.executable)
    if not (py.parent / "sentience-mcp-server").is_file():
        pytest.skip("sentience-mcp-server console script not installed beside this interpreter")
    ok, out = gate.smoke_mcp(py)
    assert ok, out
