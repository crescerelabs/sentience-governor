"""v0.2.7 CP1 — bundled Claude Code SKILL.md files + parser checks.

LOAD-BEARING. Verifies the six bundled skills are well-formed and honor
the locked plan decisions, BEFORE any installer code exists:

  - D3  — all six set ``disable-model-invocation: true``.
  - D2/§6 — the five shellout bash lines match the §6 mapping exactly.
  - D9  — no skill accepts a session-selection argument (latest-only).
  - D12 — no shellout invokes a mutating CLI command (read-only).
  - D4  — shellout ``allowed-tools`` are scoped to ``sentience``,
          never ``Bash(*)``; the static help skill has none.
  - D11/§7 Shape B — ``sentience-help`` carries the required content.
  - R4  — every SKILL.md ships as an importable package resource.
"""

from __future__ import annotations

import re
from importlib import resources

import pytest
import yaml

# §6 mapping — the exact command each shellout skill runs.
SHELLOUT_SKILLS = {
    "sentience-pulse": "sentience pulse --latest --no-prompt",
    "sentience-status": "sentience status",
    "sentience-profile": "sentience profile view",
    "sentience-violations": "sentience analyze policy-violations --latest --no-prompt",
    "sentience-intent": "sentience analyze undeclared-intent --latest --no-prompt",
}
STATIC_SKILLS = {"sentience-help"}
ALL_SKILLS = sorted(set(SHELLOUT_SKILLS) | STATIC_SKILLS)

# D9: tokens that would mean session selection / enumeration.
FORBIDDEN_D9 = re.compile(
    r"(--list|--search|--history|--aggregate|--session\b|\blist\b|\bsearch\b)"
)
# D12: mutating verbs that must never appear in a read-only shellout.
FORBIDDEN_D12 = re.compile(
    r"\b(edit|set|delete|add|remove|sync|upload)\b"
)

_BASH_LINE = re.compile(r"^!`([^`]+)`\s*$", re.MULTILINE)
_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


def _read_skill(name: str) -> str:
    """Read a bundled SKILL.md via importlib.resources (exercises R4)."""
    ref = resources.files("sentience_governor").joinpath(
        f"data/skills/{name}/SKILL.md"
    )
    return ref.read_text(encoding="utf-8")


def _split(text: str):
    m = _FRONTMATTER.match(text)
    assert m, "SKILL.md must start with a --- YAML frontmatter block ---"
    return yaml.safe_load(m.group(1)), m.group(2)


@pytest.mark.parametrize("name", ALL_SKILLS)
def test_skill_resource_ships_and_parses(name):
    """R4 — each skill is importable and has valid YAML frontmatter."""
    fm, _body = _split(_read_skill(name))
    assert isinstance(fm, dict)
    assert fm["name"] == name


@pytest.mark.parametrize("name", ALL_SKILLS)
def test_all_skills_are_operator_only(name):
    """D3 — every shipped skill disables model invocation."""
    fm, _ = _split(_read_skill(name))
    assert fm.get("disable-model-invocation") is True


@pytest.mark.parametrize("name,expected", sorted(SHELLOUT_SKILLS.items()))
def test_shellout_bash_line_matches_mapping(name, expected):
    """D2/§6 — the bash line is exactly the mapped CLI command."""
    fm, body = _split(_read_skill(name))
    lines = _BASH_LINE.findall(body)
    assert len(lines) == 1, f"{name} must have exactly one !`...` line"
    assert lines[0].strip() == expected


@pytest.mark.parametrize("name,expected", sorted(SHELLOUT_SKILLS.items()))
def test_shellout_is_latest_only_and_read_only(name, expected):
    """D9 + D12 — no session-selection arg, no mutating verb."""
    assert not FORBIDDEN_D9.search(expected), f"{name} crosses the D9 line"
    assert not FORBIDDEN_D12.search(expected), f"{name} crosses the D12 line"


@pytest.mark.parametrize("name", sorted(SHELLOUT_SKILLS))
def test_shellout_allowed_tools_scoped(name):
    """D4 — allowed-tools is scoped to sentience, never Bash(*)."""
    fm, _ = _split(_read_skill(name))
    tools = fm["allowed-tools"]
    assert tools.startswith("Bash(sentience ")
    assert "Bash(*)" not in tools


def test_static_help_has_no_shellout_and_no_allowed_tools():
    """Shape B — help is static: no !`...` line, no allowed-tools."""
    fm, body = _split(_read_skill("sentience-help"))
    assert not _BASH_LINE.findall(body)
    assert "allowed-tools" not in fm


def test_help_content_requirements():
    """D11/§7 — help lists the skills, D9 boundaries, MCP routing,
    no_signal hint, and the required install path."""
    body = _split(_read_skill("sentience-help"))[1]
    for slash in (
        "/sentience-pulse",
        "/sentience-status",
        "/sentience-profile",
        "/sentience-violations",
        "/sentience-intent",
    ):
        assert slash in body
    assert "No historical session browsing" in body
    assert "No cross-session aggregation" in body
    assert "MCP release" in body          # Claude-initiated routing note
    assert "no_signal" in body            # troubleshooting hint
    assert "pipx install sentience-governor" in body  # D11 install path


@pytest.mark.parametrize("name", sorted(SHELLOUT_SKILLS))
def test_shellout_carries_verbatim_render_instruction(name):
    """FIX-2 (v0.2.8) — the two-layer contract, in the skill body:
    render verbatim; show no_signal as-is; brand-boundary on Sentience
    headings; labeled interpretation only on explicit ask."""
    _, body = _split(_read_skill(name))
    assert "Render the command output above verbatim" in body
    assert "show it as-is and stop" in body
    # Brand boundary — the F9 counter (no fabricated branded reports).
    # (Wrap-safe: the body hard-wraps; assert on a single-line span.)
    assert "headings unless they appear in the command output" in body
    # The single standardized interpretation marker (§2.7).
    assert "Interpretation (not Sentience output):" in body


@pytest.mark.parametrize("name", ALL_SKILLS)
def test_no_skill_references_raw_trace_paths(name):
    """v0.2.8 explicit non-goal — no skill text may encourage reading
    the raw trace tree as a fallback for the CLI."""
    text = _read_skill(name)
    assert ".sentience/traces" not in text


def test_exactly_six_skills_bundled():
    """No stray skills ship beyond the six in the locked §6 set."""
    skills_dir = resources.files("sentience_governor").joinpath("data/skills")
    shipped = sorted(
        p.name for p in skills_dir.iterdir()
        if p.is_dir() and p.joinpath("SKILL.md").is_file()
    )
    assert shipped == ALL_SKILLS
