"""Unit tests for analyzer renderers (v0.2.6 CP2).

This test file extracts/adds the dedicated renderer test surface called
for in plan v3.5 fix #1. Existing renderer coverage for the v0.2.4
undeclared-intent renderers lives in ``tests/test_analyze_undeclared_intent.py``
and remains there to avoid disrupting v0.2.4 regression guards. This
file adds the v0.2.6 CP2 burn-rate renderer tests:

    render_burn_rate_cli(result, color=True)
    render_burn_rate_markdown(result)

Per CP2 sanity gates (plan v3.6):

* CLI renderer produces output that fits within 80 columns at the
  widest row.
* Markdown renderer output is valid Markdown (basic structural
  validity).
* Renderers handle all five burn-rate status paths:
  ok, no_violations, no_token_data, no_turns, partial.
* Renderers are pure (no fs / env / network / input mutation).

Golden-file tests capture byte-identical reproduction across runs via
the analyzer fixtures in ``tests/fixtures/burn_rate/``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from sentience_governor.analyze.policy_violation_burn_rate import (
    compute_policy_violation_burn_rate,
)
from sentience_governor.analyze.renderers import (
    STATUS_NO_TOKEN_DATA,
    STATUS_NO_TURNS,
    STATUS_NO_VIOLATIONS,
    STATUS_OK,
    STATUS_PARTIAL,
    render_burn_rate_cli,
    render_burn_rate_markdown,
)


_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "burn_rate"
_CLI_WIDTH_LIMIT = 80


def _load_and_compute(fixture_name: str) -> Dict[str, Any]:
    """Load a fixture JSONL and compute the burn-rate result."""
    events: List[Any] = []
    with open(_FIXTURES_DIR / fixture_name) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append(line)
    return compute_policy_violation_burn_rate(events)


# ---------------------------------------------------------------------------
# CLI renderer — status-branch coverage
# ---------------------------------------------------------------------------


class TestRenderBurnRateCliStatusBranches:
    @pytest.mark.parametrize(
        "fixture,expected_status",
        [
            ("clean.jsonl", STATUS_NO_VIOLATIONS),
            ("pol_001_only.jsonl", STATUS_OK),
            ("mixed_violations.jsonl", STATUS_OK),
            ("no_token_data.jsonl", STATUS_NO_TOKEN_DATA),
            ("no_turns.jsonl", STATUS_NO_TURNS),
            ("malformed_events.jsonl", STATUS_PARTIAL),
        ],
    )
    def test_each_fixture_renders_without_raising(self, fixture, expected_status):
        result = _load_and_compute(fixture)
        assert result["status"] == expected_status
        out = render_burn_rate_cli(result)
        assert isinstance(out, str)
        assert len(out) > 0
        assert out.endswith("\n")

    def test_no_violations_uses_encouraging_copy(self):
        result = _load_and_compute("clean.jsonl")
        out = render_burn_rate_cli(result)
        # Must lead with the status notice for operator clarity.
        assert "Status: no_violations" in out
        # Encouraging close, not blank-page energy.
        assert "no policy violations were recorded" in out.lower()
        # NO violation-causing language (per plan v3.1 wording discipline).
        assert "blocked" not in out.lower()

    def test_no_token_data_leads_with_live_session_cause(self):
        # FIX-1 (v0.2.8): empty states name the dominant real cause —
        # the session may still be running; data lands at SessionEnd.
        result = _load_and_compute("no_token_data.jsonl")
        out = render_burn_rate_cli(result)
        assert "Status: no_token_data" in out
        assert "when the Claude Code session" in out
        assert "may still be running" in out
        # The old copy led with "may not be wired" (findings F6/F13) —
        # wiring must be the fallback, never the lead.
        assert "may not be wired" not in out

    def test_no_turns_leads_with_live_session_cause(self):
        result = _load_and_compute("no_turns.jsonl")
        out = render_burn_rate_cli(result)
        assert "Status: no_turns" in out
        assert "may still be running" in out
        assert "sentience init claude-code" in out

    def test_no_turns_reports_measured_rule_counts(self):
        """FIX-4 (v0.2.8): the no_turns surface shows which rules fired
        (measured), with token attribution explicitly deferred."""
        result = _load_and_compute("no_turns.jsonl")
        out = render_burn_rate_cli(result)
        assert "Rules fired in this session" in out
        assert "token attribution pending" in out
        assert "POL-001  2" in out

    def test_partial_surfaces_warning_count(self):
        result = _load_and_compute("malformed_events.jsonl")
        out = render_burn_rate_cli(result)
        assert "status=partial" in out
        assert "trace warning" in out
        assert "--json" in out


class TestRenderBurnRateCliOkPath:
    def test_per_rule_rows_in_output(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_cli(result)
        for rule in ("POL-001", "POL-002", "POL-003", "POL-004", "POL-005"):
            assert rule in out, f"missing rule {rule} in CLI output"

    def test_inspection_target_line_present(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_cli(result)
        # "X appeared on turns representing ~N tokens of session compute."
        assert "appeared on turns representing" in out
        assert "good place to inspect first" in out

    def test_non_additivity_note_when_multi_rule(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_cli(result)
        assert "not additive" in out

    def test_no_non_additivity_note_when_single_rule(self):
        result = _load_and_compute("pol_001_only.jsonl")
        out = render_burn_rate_cli(result)
        assert "not additive" not in out

    def test_profile_footnote_when_profile_loaded(self):
        result = _load_and_compute("pol_001_only.jsonl")
        out = render_burn_rate_cli(result)
        assert "Profile: fingerprint" in out


class TestRenderBurnRateCliAttributionDiscipline:
    """Plan v3.6 + v2 wording discipline: burn-rate copy uses
    association language only, never savings/causality.
    """

    @pytest.mark.parametrize(
        "fixture",
        ["pol_001_only.jsonl", "mixed_violations.jsonl", "malformed_events.jsonl"],
    )
    def test_no_savings_language(self, fixture):
        result = _load_and_compute(fixture)
        out = render_burn_rate_cli(result).lower()
        banned = ["reclaim", "would save", "could save", "would prevent",
                  "would have been"]
        for word in banned:
            assert word not in out, (
                f"banned attribution-discipline word {word!r} in {fixture} "
                f"CLI output"
            )


# ---------------------------------------------------------------------------
# CLI renderer — 80-col width compliance (CP2 sanity gate)
# ---------------------------------------------------------------------------


class TestRenderBurnRateCliWidth:
    @pytest.mark.parametrize(
        "fixture",
        [
            "clean.jsonl",
            "pol_001_only.jsonl",
            "mixed_violations.jsonl",
            "no_token_data.jsonl",
            "no_turns.jsonl",
            "malformed_events.jsonl",
        ],
    )
    def test_no_line_exceeds_80_cols(self, fixture):
        result = _load_and_compute(fixture)
        out = render_burn_rate_cli(result)
        for i, line in enumerate(out.rstrip("\n").split("\n")):
            assert len(line) <= _CLI_WIDTH_LIMIT, (
                f"line {i} of {fixture} CLI output is {len(line)} cols "
                f"(limit {_CLI_WIDTH_LIMIT}): {line!r}"
            )


# ---------------------------------------------------------------------------
# Markdown renderer — status-branch coverage
# ---------------------------------------------------------------------------


class TestRenderBurnRateMarkdownStatusBranches:
    @pytest.mark.parametrize(
        "fixture,expected_status",
        [
            ("clean.jsonl", STATUS_NO_VIOLATIONS),
            ("pol_001_only.jsonl", STATUS_OK),
            ("mixed_violations.jsonl", STATUS_OK),
            ("no_token_data.jsonl", STATUS_NO_TOKEN_DATA),
            ("no_turns.jsonl", STATUS_NO_TURNS),
            ("malformed_events.jsonl", STATUS_PARTIAL),
        ],
    )
    def test_each_fixture_renders_without_raising(self, fixture, expected_status):
        result = _load_and_compute(fixture)
        assert result["status"] == expected_status
        out = render_burn_rate_markdown(result)
        assert isinstance(out, str)
        assert len(out) > 0
        assert out.endswith("\n")

    def test_starts_with_h1_title(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_markdown(result)
        assert out.startswith("# Policy-Violation Burn Rate")

    def test_no_violations_includes_interpretation_section(self):
        result = _load_and_compute("clean.jsonl")
        out = render_burn_rate_markdown(result)
        assert "## Interpretation" in out
        assert "no policy violations were recorded" in out.lower()

    def test_no_token_data_includes_canonical_footer(self):
        result = _load_and_compute("no_token_data.jsonl")
        out = render_burn_rate_markdown(result)
        assert "operators@crescerelabs.com" in out
        assert "launch-list" in out

    def test_no_turns_includes_canonical_footer(self):
        result = _load_and_compute("no_turns.jsonl")
        out = render_burn_rate_markdown(result)
        assert "operators@crescerelabs.com" in out

    def test_partial_includes_notes_section(self):
        result = _load_and_compute("malformed_events.jsonl")
        out = render_burn_rate_markdown(result)
        assert "## Notes" in out
        assert "partial" in out


class TestRenderBurnRateMarkdownOkPath:
    def test_by_rule_table_present(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_markdown(result)
        assert "## By rule" in out
        assert "| Rule | Turns | Tokens | Description |" in out

    def test_all_five_rules_in_table(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_markdown(result)
        for rule in ("POL-001", "POL-002", "POL-003", "POL-004", "POL-005"):
            assert f"`{rule}`" in out

    def test_non_additivity_note_when_multi_rule(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_markdown(result)
        # Markdown uses the full `notes` text.
        assert "not additive" in out
        assert "expected, not a bug" in out

    def test_no_non_additivity_note_when_single_rule(self):
        result = _load_and_compute("pol_001_only.jsonl")
        out = render_burn_rate_markdown(result)
        assert "not additive" not in out

    def test_sample_turns_section_present(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_markdown(result)
        assert "## Sample turns per rule" in out

    def test_profile_section_when_profile_loaded(self):
        result = _load_and_compute("pol_001_only.jsonl")
        out = render_burn_rate_markdown(result)
        assert "## Profile" in out
        assert "Profile fingerprint" in out

    def test_operational_interpretation_section_present(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_markdown(result)
        assert "## Operational interpretation" in out
        assert "appeared on turns representing" in out

    def test_canonical_footer_present(self):
        result = _load_and_compute("mixed_violations.jsonl")
        out = render_burn_rate_markdown(result)
        assert "operators@crescerelabs.com" in out
        assert "launch-list" in out


class TestRenderBurnRateMarkdownAttributionDiscipline:
    @pytest.mark.parametrize(
        "fixture",
        ["pol_001_only.jsonl", "mixed_violations.jsonl", "malformed_events.jsonl"],
    )
    def test_no_savings_language(self, fixture):
        result = _load_and_compute(fixture)
        out = render_burn_rate_markdown(result).lower()
        banned = ["reclaim", "would save", "could save", "would prevent",
                  "would have been"]
        for word in banned:
            assert word not in out, (
                f"banned word {word!r} in {fixture} Markdown output"
            )


# ---------------------------------------------------------------------------
# Markdown renderer — basic structural validity (CP2 sanity gate)
# ---------------------------------------------------------------------------


class TestRenderBurnRateMarkdownStructure:
    @pytest.mark.parametrize(
        "fixture",
        [
            "clean.jsonl",
            "pol_001_only.jsonl",
            "mixed_violations.jsonl",
            "no_token_data.jsonl",
            "no_turns.jsonl",
            "malformed_events.jsonl",
        ],
    )
    def test_balanced_headings_and_no_runaway_lines(self, fixture):
        result = _load_and_compute(fixture)
        out = render_burn_rate_markdown(result)
        # At least one H1 heading.
        assert out.count("# ") >= 1
        # No suspiciously long lines (>500 chars suggests an unterminated
        # string or a missing newline).
        for line in out.split("\n"):
            assert len(line) < 500, (
                f"runaway Markdown line in {fixture}: {line[:120]!r}..."
            )

    @pytest.mark.parametrize(
        "fixture", ["pol_001_only.jsonl", "mixed_violations.jsonl"]
    )
    def test_tables_have_header_separator(self, fixture):
        """Markdown tables must include the |---|---|... separator
        row between header and body — verify the By rule table has
        one."""
        result = _load_and_compute(fixture)
        out = render_burn_rate_markdown(result)
        lines = out.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("| Rule"):
                # Next non-empty line should be the separator.
                assert i + 1 < len(lines)
                sep = lines[i + 1]
                assert sep.startswith("|") and "---" in sep, (
                    f"missing table separator after header in {fixture}: "
                    f"line {i + 1} = {sep!r}"
                )
                break
        else:
            pytest.fail(f"no By-rule table found in {fixture}")


# ---------------------------------------------------------------------------
# Renderer purity + replay stability (golden-file equivalent)
# ---------------------------------------------------------------------------


class TestRenderBurnRateReplayStability:
    @pytest.mark.parametrize(
        "fixture",
        [
            "clean.jsonl",
            "pol_001_only.jsonl",
            "mixed_violations.jsonl",
            "no_token_data.jsonl",
            "no_turns.jsonl",
            "malformed_events.jsonl",
        ],
    )
    def test_cli_byte_identical_on_repeated_calls(self, fixture):
        result = _load_and_compute(fixture)
        a = render_burn_rate_cli(result)
        b = render_burn_rate_cli(result)
        c = render_burn_rate_cli(result)
        assert a == b == c

    @pytest.mark.parametrize(
        "fixture",
        [
            "clean.jsonl",
            "pol_001_only.jsonl",
            "mixed_violations.jsonl",
            "no_token_data.jsonl",
            "no_turns.jsonl",
            "malformed_events.jsonl",
        ],
    )
    def test_markdown_byte_identical_on_repeated_calls(self, fixture):
        result = _load_and_compute(fixture)
        a = render_burn_rate_markdown(result)
        b = render_burn_rate_markdown(result)
        c = render_burn_rate_markdown(result)
        assert a == b == c

    def test_renderers_do_not_mutate_input(self):
        result = _load_and_compute("mixed_violations.jsonl")
        snapshot = copy.deepcopy(result)
        render_burn_rate_cli(result)
        render_burn_rate_markdown(result)
        assert result == snapshot


class TestRenderBurnRateEdgeCases:
    def test_empty_result_dict_renders_without_raising(self):
        """A minimal/empty result dict should not crash either renderer."""
        result: Dict[str, Any] = {"status": STATUS_NO_TURNS, "session_id": ""}
        cli = render_burn_rate_cli(result)
        md = render_burn_rate_markdown(result)
        assert "no_turns" in cli
        assert "no_turns" in md

    def test_unknown_status_falls_through_to_full_render(self):
        """An unrecognized status string should still render via the
        ok/partial path rather than raising."""
        result: Dict[str, Any] = {
            "status": "weird_unknown_status",
            "session_id": "test-session",
            "total_tokens": 100,
            "violation_associated_tokens": 50,
            "violation_firing_turns": 1,
            "by_rule": {},
            "notes": [],
            "notes_short": [],
        }
        # Should render via the ok/partial path without raising.
        cli = render_burn_rate_cli(result)
        md = render_burn_rate_markdown(result)
        assert "test-ses" in cli
        assert "test-ses" in md

    def test_cli_color_kwarg_currently_no_op(self):
        """The color kwarg is reserved for future ANSI support; today
        the output should be identical with color=True and color=False."""
        result = _load_and_compute("mixed_violations.jsonl")
        a = render_burn_rate_cli(result, color=True)
        b = render_burn_rate_cli(result, color=False)
        assert a == b


# ===========================================================================
# v0.2.6 CP5 — Pulse renderer tests
# ===========================================================================
#
# Coverage targets (per plan v3.6 §CP5 test list):
#
# * All four pulse statuses (ok / partial / limited / no_signal).
# * All three v0.2.6 sync_prompt.reason values (not_subscribed shows
#   footer; already_subscribed + opted_out suppress it).
# * Clean-session pulse output includes the mandatory Interpretation
#   block with the qualified v3.1 wording.
# * Violation-bearing pulse output includes the non-additivity note
#   inline between by-rule rows and the why-it-matters line.
# * 80-col CLI sanity gate.
# * Every section's "why it matters" line renders.
# * Renderer-purity test: identical output from a sandboxed
#   environment (no env vars, no filesystem).
# * Color kwarg is no-op (reserved).


from sentience_governor.analyze import compute_pulse  # noqa: E402
from sentience_governor.analyze.renderers import (  # noqa: E402
    PULSE_STATUS_LIMITED,
    PULSE_STATUS_NO_SIGNAL,
    PULSE_STATUS_OK,
    PULSE_STATUS_PARTIAL,
    _pulse_tool_token_attr_lines,
    _pulse_tool_token_attr_md_lines,
    render_pulse_cli,
    render_pulse_markdown,
)


def _pulse_for_fixture(fixture_name: str) -> Dict[str, Any]:
    """Load a fixture and compose a full pulse result."""
    events: List[Any] = []
    with open(_FIXTURES_DIR / fixture_name) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append(line)
    return compute_pulse(events)


def _attach_sync_prompt(result: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """Helper: simulate what the CP6 CLI handler does — attach a
    realistic ``sync_prompt`` dict before passing to the renderer.
    """
    out = copy.deepcopy(result)
    if reason == "not_subscribed":
        out["sync_prompt"] = {"show": True, "reason": "not_subscribed"}
    elif reason == "already_subscribed":
        out["sync_prompt"] = {"show": False, "reason": "already_subscribed"}
    elif reason == "opted_out":
        out["sync_prompt"] = {"show": False, "reason": "opted_out"}
    elif reason == "uninitialized":
        out["sync_prompt"] = {"show": False, "reason": "uninitialized"}
    else:
        raise AssertionError(f"unknown sync_prompt reason {reason!r}")
    return out


class TestPulseTokenBreakdownIR4:
    """IR-4 + IR-2 (v0.2.8.1): the pulse surfaces the four token classes and
    states the dedupe semantics; the breakdown reconciles to total compute."""

    def test_pulse_cli_shows_four_classes_and_dedupe(self):
        out = render_pulse_cli(_pulse_for_fixture("mixed_violations.jsonl"))
        assert "Token classes (sum to total compute):" in out
        assert "cached read" in out
        assert "cached write" in out
        assert "prompt" in out
        assert "completion" in out
        assert "Per-turn usage is deduped by requestId." in out

    def test_token_breakdown_reconciles_to_total(self):
        undeclared = _pulse_for_fixture(
            "mixed_violations.jsonl"
        )["undeclared_intent"]
        bd = undeclared["token_breakdown"]
        assert (
            bd["prompt"] + bd["completion"]
            + bd["cached_read"] + bd["cached_write"]
            == undeclared["total_tokens"]
        )

    def test_pulse_markdown_shows_breakdown_and_dedupe(self):
        md = render_pulse_markdown(_pulse_for_fixture("mixed_violations.jsonl"))
        assert "Cached read" in md
        assert "Per-turn usage is deduped by requestId." in md


# ---------------------------------------------------------------------------
# 1. Pulse status-branch coverage (CLI)
# ---------------------------------------------------------------------------


class TestPulseCliStatusBranches:
    def test_status_ok_renders_full_breakdown(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        result = _attach_sync_prompt(result, "not_subscribed")
        assert result["status"] == PULSE_STATUS_OK
        out = render_pulse_cli(result)
        assert "Sentience Pulse" in out
        assert "Undeclared-intent spend" in out
        assert "Policy-violation burn rate" in out
        assert "Advisory flags" in out
        # The five expected sections including the header line all
        # appear in the rendered output.

    def test_status_no_signal_renders_terse_framing(self):
        result = _pulse_for_fixture("no_turns.jsonl")
        assert result["status"] == PULSE_STATUS_NO_SIGNAL
        out = render_pulse_cli(result)
        assert "No usable analyzer signal" in out
        # No per-section breakdown when no_signal.
        assert "Undeclared-intent spend" not in out
        assert "Policy-violation burn rate" not in out

    def test_status_limited_prepends_specific_notice(self):
        """Construct a synthetic limited-status result by hand:
        undeclared=ok, burn=no_token_data → usable_ok + limited_signal
        → limited."""
        result = _pulse_for_fixture("mixed_violations.jsonl")
        result["status"] = PULSE_STATUS_LIMITED
        # Make burn-rate look limited by swapping in a no_token_data
        # status so _count_usable_sub_analyzers reports 1 of 2.
        result["policy_violations_burn_rate"]["status"] = "no_token_data"
        out = render_pulse_cli(result)
        assert "Limited signal — 1 of 2 analyzers" in out

    def test_status_partial_renders_advisory(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        result["status"] = PULSE_STATUS_PARTIAL
        out = render_pulse_cli(result)
        assert "status=partial" in out


# ---------------------------------------------------------------------------
# 2. Clean-session output discipline (Interpretation block + wording)
# ---------------------------------------------------------------------------


class TestPulseCleanSessionDiscipline:
    def test_clean_session_renders_interpretation_block(self):
        result = _pulse_for_fixture("clean.jsonl")
        out = render_pulse_cli(result)
        assert "Interpretation" in out
        # Plan v3.1 qualified wording — locked. Collapse newlines for
        # the substring check since the renderer wraps the block.
        flat = " ".join(out.split())
        assert (
            "no policy violations were recorded against the rules active"
            in flat
        )

    def test_clean_session_does_not_overclaim(self):
        """Regression guard against plan v3.1 wording discipline —
        the renderer MUST NOT use overclaiming phrases."""
        result = _pulse_for_fixture("clean.jsonl")
        out = render_pulse_cli(result)
        banned = [
            "stayed within all meaningful boundaries",
            "agent stayed within the boundaries you authored",
            "guaranteed",
            "proven safe",
        ]
        for word in banned:
            assert word not in out, (
                f"banned overclaiming phrase {word!r} found"
            )

    def test_clean_session_markdown_includes_interpretation(self):
        result = _pulse_for_fixture("clean.jsonl")
        md = render_pulse_markdown(result)
        assert "## Interpretation" in md
        flat = " ".join(md.split())
        assert (
            "no policy violations were recorded against the rules active"
            in flat
        )


# ---------------------------------------------------------------------------
# 3. Non-additivity note placement (violation-bearing path)
# ---------------------------------------------------------------------------


class TestPulseNonAdditivityNote:
    def test_cli_non_additivity_note_between_rules_and_why(self):
        """Plan v3.3 CP2 contract: when more than one rule has
        non-zero token_cost, the non-additivity note must appear
        inline between the by-rule rows and the why-it-matters
        line.
        """
        result = _pulse_for_fixture("mixed_violations.jsonl")
        out = render_pulse_cli(result)
        # The non-additivity note text (from notes_short).
        note_idx = out.find("not additive")
        why_idx = out.find("Why it matters: POL-")
        assert note_idx != -1, "non-additivity note missing"
        assert why_idx != -1, "why-it-matters line missing"
        assert note_idx < why_idx, (
            "non-additivity note must appear BEFORE the why-it-matters "
            "line (plan v3.3 CP2 contract)"
        )


# ---------------------------------------------------------------------------
# 4. 80-column sanity gate
# ---------------------------------------------------------------------------


class TestPulseCli80ColCompliance:
    @pytest.mark.parametrize(
        "fixture_name",
        [
            "clean.jsonl",
            "pol_001_only.jsonl",
            "mixed_violations.jsonl",
            "no_token_data.jsonl",
            "no_turns.jsonl",
        ],
    )
    def test_cli_fits_within_80_cols(self, fixture_name):
        result = _pulse_for_fixture(fixture_name)
        result = _attach_sync_prompt(result, "not_subscribed")
        out = render_pulse_cli(result)
        wide = [
            (i + 1, len(line))
            for i, line in enumerate(out.splitlines())
            if len(line) > _CLI_WIDTH_LIMIT
        ]
        assert wide == [], (
            f"{fixture_name}: {len(wide)} CLI line(s) exceeded 80 cols: "
            f"{wide}"
        )


# ---------------------------------------------------------------------------
# 5. "Why it matters" presence — every section
# ---------------------------------------------------------------------------


class TestPulseWhyItMattersCoverage:
    """Plan v3 60-second-value mandate: every pulse section ends in a
    'Why it matters' interpretive line."""

    def test_violation_bearing_each_section_has_why_it_matters(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        out = render_pulse_cli(result)
        # Three sections (undeclared-intent, burn rate, advisory flags)
        # → at least three 'Why it matters' lines.
        assert out.count("Why it matters:") >= 3

    def test_clean_session_each_section_has_why_it_matters(self):
        result = _pulse_for_fixture("clean.jsonl")
        out = render_pulse_cli(result)
        assert out.count("Why it matters:") >= 3


# ---------------------------------------------------------------------------
# 6. Sync-prompt footer eligibility (renderer reads only show flag)
# ---------------------------------------------------------------------------


class TestPulseSyncPromptFooter:
    """Renderer-side contract: footer shows iff
    ``result.sync_prompt.show == True``. The reason value is opaque
    to the renderer; it only inspects ``show``.
    """

    def test_not_subscribed_shows_footer(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        result = _attach_sync_prompt(result, "not_subscribed")
        out = render_pulse_cli(result)
        assert "Want this pulse delivered weekly" in out
        assert "getsentience.ai/sentience-sync" in out

    def test_already_subscribed_suppresses_footer(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        result = _attach_sync_prompt(result, "already_subscribed")
        out = render_pulse_cli(result)
        assert "Want this pulse delivered weekly" not in out
        assert "getsentience.ai/sentience-sync" not in out

    def test_opted_out_suppresses_footer(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        result = _attach_sync_prompt(result, "opted_out")
        out = render_pulse_cli(result)
        assert "Want this pulse delivered weekly" not in out

    def test_uninitialized_default_suppresses_footer(self):
        # compute_pulse default — no CLI handler attached eligibility.
        result = _pulse_for_fixture("mixed_violations.jsonl")
        out = render_pulse_cli(result)
        assert "Want this pulse delivered weekly" not in out

    def test_markdown_footer_eligibility_matches_cli(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        shown = _attach_sync_prompt(result, "not_subscribed")
        suppressed = _attach_sync_prompt(result, "already_subscribed")
        md_shown = render_pulse_markdown(shown)
        md_suppressed = render_pulse_markdown(suppressed)
        assert "getsentience.ai/sentience-sync" in md_shown
        assert "getsentience.ai/sentience-sync" not in md_suppressed


# ---------------------------------------------------------------------------
# 7. Markdown structural validity
# ---------------------------------------------------------------------------


class TestPulseMarkdownStructure:
    def test_markdown_has_top_level_heading(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        md = render_pulse_markdown(result)
        assert md.startswith("# Sentience Pulse — session")

    def test_markdown_section_headings_in_order(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        md = render_pulse_markdown(result)
        idx_u = md.find("## Undeclared-intent spend")
        idx_b = md.find("## Policy-violation burn rate")
        idx_a = md.find("## Advisory flags")
        assert -1 < idx_u < idx_b < idx_a, (
            "Markdown section headings must appear in canonical order"
        )

    def test_clean_session_markdown_section_order_includes_interpretation(self):
        result = _pulse_for_fixture("clean.jsonl")
        md = render_pulse_markdown(result)
        idx_a = md.find("## Advisory flags")
        idx_i = md.find("## Interpretation")
        assert -1 < idx_a < idx_i


# ---------------------------------------------------------------------------
# 8. Replay-stability / renderer-purity contract
# ---------------------------------------------------------------------------


class TestPulseRendererPurity:
    @pytest.mark.parametrize(
        "fixture_name",
        [
            "clean.jsonl",
            "pol_001_only.jsonl",
            "mixed_violations.jsonl",
            "no_turns.jsonl",
        ],
    )
    def test_repeated_calls_byte_identical(self, fixture_name):
        result = _pulse_for_fixture(fixture_name)
        a = render_pulse_cli(result)
        b = render_pulse_cli(result)
        assert a == b
        md_a = render_pulse_markdown(result)
        md_b = render_pulse_markdown(result)
        assert md_a == md_b

    def test_renderer_does_not_mutate_input(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        before = copy.deepcopy(result)
        render_pulse_cli(result)
        render_pulse_markdown(result)
        assert result == before

    def test_renderer_purity_sandbox(self, monkeypatch, tmp_path):
        """Renderer-purity test per plan v3.6 §CP5: identical output
        from a sandboxed environment (no env vars, empty cwd) vs the
        baseline.
        """
        import os as _os
        result = _pulse_for_fixture("mixed_violations.jsonl")
        result = _attach_sync_prompt(result, "not_subscribed")
        baseline_cli = render_pulse_cli(result)
        baseline_md = render_pulse_markdown(result)
        with monkeypatch.context() as m:
            for var in list(_os.environ.keys()):
                m.delenv(var, raising=False)
            m.chdir(tmp_path)
            sandboxed_cli = render_pulse_cli(result)
            sandboxed_md = render_pulse_markdown(result)
        assert baseline_cli == sandboxed_cli
        assert baseline_md == sandboxed_md


# ---------------------------------------------------------------------------
# 9. Color kwarg
# ---------------------------------------------------------------------------


class TestPulseCliColorKwarg:
    def test_color_kwarg_is_currently_no_op(self):
        result = _pulse_for_fixture("mixed_violations.jsonl")
        a = render_pulse_cli(result, color=True)
        b = render_pulse_cli(result, color=False)
        assert a == b


# ---------------------------------------------------------------------------
# 10. F21 (v0.2.9) — tool calls as a first-class pulse field
# ---------------------------------------------------------------------------


class TestPulseToolCallsF21:
    """F21: tool calls surface on both pulse surfaces. The mixed_violations
    fixture has two tool calls (fs.read READ, fs.write WRITE)."""

    def test_cli_renders_tool_calls_block(self):
        out = render_pulse_cli(_pulse_for_fixture("mixed_violations.jsonl"))
        assert "Tool calls (2 total):" in out
        lines = [ln.strip() for ln in out.splitlines()]
        assert any(ln.startswith("read") and ln.endswith("1") for ln in lines)
        assert any(ln.startswith("write") and ln.endswith("1") for ln in lines)
        assert "Top tools by call count:" in out
        assert "fs.write (1)" in out

    def test_markdown_renders_tool_calls_table(self):
        md = render_pulse_markdown(_pulse_for_fixture("mixed_violations.jsonl"))
        assert "### Tool calls" in md
        assert "| **Total** | **2** |" in md
        assert "| Read | 1 |" in md
        assert "| Write | 1 |" in md
        assert "_Top tools by call count:" in md


# ---------------------------------------------------------------------------
# 11. IR-3 (v0.2.9) — measured tool-token attribution (A1 + A2)
# ---------------------------------------------------------------------------


def _attr_sub() -> Dict[str, Any]:
    """An undeclared_intent sub-result with a populated IR-3 block:
    A1 = 1000 of 1500 (66.7%); Bash + Edit each credited the full 1000
    (non-additive)."""
    return {
        "tool_token_attribution": {
            "tokens_on_turns_with_tool_calls": 1000,
            "total_tokens": 1500,
            "percent_of_total": 66.7,
            "by_tool": [
                {"tool_id": "Bash", "tokens": 1000, "turn_count": 1},
                {"tool_id": "Edit", "tokens": 1000, "turn_count": 1},
            ],
            "by_tool_is_non_additive": True,
        }
    }


class TestPulseToolTokenAttrIR3:
    def test_cli_a1_headline_uses_turn_attributed_label(self):
        out = "\n".join(_pulse_tool_token_attr_lines(_attr_sub()))
        assert (
            "Tokens on turns that fired ≥1 tool call: 1,000 (66.7% of total)"
            in out
        )

    def test_cli_a2_lines_are_turn_attributed_and_non_additive(self):
        out = "\n".join(_pulse_tool_token_attr_lines(_attr_sub()))
        assert "Tokens on turns involving each tool" in out
        assert "Bash: 1,000 on 1 turns" in out
        assert "Edit: 1,000 on 1 turns" in out
        assert "non-additive" in out.lower()

    def test_cli_wording_guard_never_per_tool_spend(self):
        """The whole product-integrity line: no per-tool 'spent/cost'."""
        out = "\n".join(_pulse_tool_token_attr_lines(_attr_sub())).lower()
        assert "spent" not in out
        assert "spend" not in out
        assert "cost" not in out

    def test_markdown_table_and_disclaimer(self):
        md = "\n".join(_pulse_tool_token_attr_md_lines(_attr_sub()))
        assert "### Tool-token attribution" in md
        assert "Tokens on turns that fired ≥1 tool call:" in md
        assert "| Tokens on its turns |" in md
        assert "| Bash | 1,000 | 1 |" in md
        assert "non-additive" in md.lower()

    def test_markdown_wording_guard_never_per_tool_spend(self):
        md = "\n".join(_pulse_tool_token_attr_md_lines(_attr_sub())).lower()
        assert "spent" not in md and "spend" not in md and "cost" not in md

    def test_both_surfaces_empty_without_token_data(self):
        assert _pulse_tool_token_attr_lines(
            {"tool_token_attribution": {"total_tokens": 0}}
        ) == []
        assert _pulse_tool_token_attr_md_lines({}) == []

    def test_end_to_end_a1_line_appears_in_full_cli_pulse(self):
        """Integration: with a real tool_use_id join, the A1 line reaches
        the full CLI pulse output."""
        events = [
            {"event_type": "INTENT_DECLARED", "session_id": "s",
             "event_sequence_number": 1, "agent_id": "a",
             "payload": {"stated_objective": "x"}},
            {"event_type": "SCOPE_ASSERTED", "session_id": "s",
             "event_sequence_number": 2, "agent_id": "a",
             "payload": {"tool_id": "Bash", "operation_type": "EXECUTE",
                         "tool_use_id": "tu1", "asserted_permissions": [],
                         "target_system": "shell"},
             "advisory_flags": [], "policy_violations": []},
            {"event_type": "CONTEXT_SNAPSHOT", "session_id": "s",
             "event_sequence_number": 3, "agent_id": "a",
             "payload": {"llm_turn_id": "t1", "tool_use_ids": ["tu1"],
                         "llm_prompt_tokens": 500, "context_size_tokens": 10},
             "advisory_flags": [], "policy_violations": []},
        ]
        out = render_pulse_cli(compute_pulse(events))
        assert "Tokens on turns that fired ≥1 tool call:" in out
        assert "Bash: 500 on 1 turns" in out
