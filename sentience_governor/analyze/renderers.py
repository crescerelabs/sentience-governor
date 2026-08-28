"""Renderers for analyzer results.

Pure-function renderers that consume analyzer result dicts and produce
surface-specific output. Two analyzer families are served:

* **Undeclared-intent** (v0.2.4): :func:`render_cli` and
  :func:`render_markdown_report` consume the dict from
  :func:`sentience_governor.analyze.undeclared_intent.compute_undeclared_intent_spend`.

* **Policy-violation burn rate** (v0.2.6 CP2):
  :func:`render_burn_rate_cli` and :func:`render_burn_rate_markdown`
  consume the dict from
  :func:`sentience_governor.analyze.policy_violation_burn_rate.compute_policy_violation_burn_rate`.

* **Pulse** (v0.2.6 CP5): :func:`render_pulse_cli` and
  :func:`render_pulse_markdown` consume the dict from
  :func:`sentience_governor.analyze.pulse.compute_pulse`. Pulse is
  the v0.2.6 adoption surface — composed of the two analyzers
  above plus an advisory-flag summary and an Interpretation block.

In each family, the CLI surface is terminal-output (one-screen-friendly,
screenshot-worthy, 80-column-safe) and does NOT print the save prompt —
that ordering is governed by the CLI handler. The Markdown surface
includes the canonical two-vector footer and is written to
``~/.sentience/reports/`` by the CLI ``--save`` flow.

These renderers are pure: no I/O, no env reads, no input mutation.
File-writing lives in the CLI handler, not here.

**Attribution discipline (burn-rate specifically, per plan v3 §v2
clarification):** burn-rate output uses **association language only**,
never savings/causality language. No "would reclaim", "could save",
"would prevent", "would have been" wording in burn-rate copy — the
metric is a deterministic prioritization signal, not a savings
estimate.

"""

from __future__ import annotations

import textwrap
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Status constants (mirrored from the analyzer module so renderers don't
# import from the analyzer — avoids circular import risk and keeps this
# module independently testable).
# ---------------------------------------------------------------------------

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_NO_TOKEN_DATA = "no_token_data"
STATUS_NO_TURNS = "no_turns"
# Burn-rate-specific status; absent from the undeclared-intent analyzer.
STATUS_NO_VIOLATIONS = "no_violations"

# Canonical footer copy — LOCKED per plan v3 §"Canonical saved-report
# footer copy". Any change here must be matched in the plan doc.
_CANONICAL_FOOTER = (
    "This is one session. A consolidated view across all your runs —\n"
    "drift trends, compute attributed across workflows, agent behavior\n"
    "compared over time — is what's next. If you'd find it useful,\n"
    "the most helpful thing is hearing what it should answer.\n"
    "\n"
    "Reply: operators@crescerelabs.com\n"
    "Or stay informed: getsentience.ai/launch-list\n"
)

# Footer line copy — LOCKED per plan v3 §"Differentiated footer copy".
_FOOTER_AGENT_BOUND = (
    "{undeclared_tokens:,} tokens were attributed to turns that touched "
    "execution outside\n"
    "this session's declared operational intent. Were these valid and\n"
    "expected? If not, policy can intervene at the execution boundary —\n"
    "review, constraint, confirmation, or block."
)

_FOOTER_SURFACE_BOUND = (
    "This session contains no declared intent — every attributed turn\n"
    "is classified as undeclared. Often this reflects a surface-level\n"
    "limitation (e.g. Claude Code hooks today, which don't yet expose\n"
    "an intent-declaration primitive) rather than agent drift."
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_cli(result: Dict[str, Any], *, color: bool = True) -> str:
    """Render an undeclared-intent result for the terminal.

    Args:
        result: The dict returned by ``compute_undeclared_intent_spend``.
        color: Reserved for future ANSI color support. Currently
            unused; the v0.2.4 surface is plain text for screenshot
            stability and PyPI README rendering.

    Returns:
        Plain-text rendering, terminated with a single newline.
        Status branches:

        * ``ok`` / ``partial`` — full breakdown + footer.
        * ``no_token_data`` — short notice; no breakdown; no footer.
        * ``no_turns`` — short notice; no breakdown; no footer.

    Pure function; does not print the save prompt.
    """
    status = result.get("status", "")
    session_id = result.get("session_id") or ""
    sid_short = session_id[:8] if session_id else "(unknown)"

    if status == STATUS_NO_TOKEN_DATA:
        return _render_no_token_data(sid_short)

    if status == STATUS_NO_TURNS:
        return _render_no_turns(sid_short)

    # ok / partial — full breakdown.
    lines: List[str] = []
    title = f"Undeclared-Intent Spend — session {sid_short}..."
    lines.append(title)
    lines.append("─" * max(len(title), 41))

    total = int(result.get("total_tokens", 0) or 0)
    undeclared = int(result.get("undeclared_tokens", 0) or 0)
    declared = int(result.get("declared_tokens", 0) or 0)
    pct = float(result.get("undeclared_percent", 0.0) or 0.0)
    declared_pct = round(100.0 - pct, 1) if total > 0 else 0.0

    lines.append(f"Total compute       {total:>9,} tokens")
    lines.append(
        f"Undeclared          {undeclared:>9,} tokens   ({pct:.1f}%)"
    )
    lines.append(
        f"Declared            {declared:>9,} tokens   ({declared_pct:.1f}%)"
    )
    lines.append("")

    undeclared_turns = result.get("undeclared_turns") or []
    if undeclared_turns:
        lines.append("Undeclared turns")
        for entry in undeclared_turns:
            lines.append(_format_turn_line(entry))
        lines.append("")

    # v0.2.5 — profile-driven sections. Each is omitted when the
    # underlying list is empty / metadata is absent, so v0.2.4-shaped
    # traces produce byte-identical CLI output (regression guard per
    # plan CP4 §"CLI render with no profile metadata: identical to
    # v0.2.4 output").
    high_consequence_events = result.get("high_consequence_events") or []
    if high_consequence_events:
        lines.append("High-consequence operations")
        for entry in high_consequence_events:
            lines.append(_format_high_consequence_line(entry))
        lines.append("")

    task_boundary_events = result.get("task_boundary_events") or []
    if task_boundary_events:
        lines.append("Task boundaries crossed")
        for entry in task_boundary_events:
            lines.append(_format_task_boundary_line(entry))
        lines.append("")

    # Profile metadata footnote — only when the trace identifies a
    # loaded operator-authored profile. The fingerprint correlates
    # this report to the operator's local profile file.
    if result.get("profile_loaded") is True:
        fp = result.get("profile_fingerprint") or "(unknown)"
        psv = result.get("profile_schema_version")
        psv_str = str(psv) if isinstance(psv, int) else "(unknown)"
        lines.append(
            f"Profile: fingerprint {fp}, schema v{psv_str}"
        )
        lines.append("")

    # Status partial — surface a small advisory above the footer.
    if status == STATUS_PARTIAL:
        warn_count = (
            int(result.get("unpaired_event_count", 0) or 0)
            + int(result.get("untokened_pair_count", 0) or 0)
            + int(result.get("dedupe_conflict_count", 0) or 0)
            + int(result.get("malformed_event_count", 0) or 0)
        )
        lines.append(
            f"Note: status=partial — {warn_count} trace warning(s) "
            "accumulated.\n"
            "Numeric attribution above is correct for the readable "
            "subset; see\n"
            "--json output for the warnings list."
        )
        lines.append("")

    # Footer line — branch on session_has_declared_intent.
    has_intent = bool(result.get("session_has_declared_intent", False))
    if has_intent:
        lines.append(_FOOTER_AGENT_BOUND.format(undeclared_tokens=undeclared))
    else:
        lines.append(_FOOTER_SURFACE_BOUND)

    return "\n".join(lines) + "\n"


def render_markdown_report(result: Dict[str, Any]) -> str:
    """Render an undeclared-intent result as a Markdown report.

    Args:
        result: The dict returned by ``compute_undeclared_intent_spend``.

    Returns:
        Markdown content suitable for writing to
        ``~/.sentience/reports/undeclared-intent-*.md``.

    Includes the canonical two-vector footer (operators@crescerelabs.com
    reply path + launch-list link) — the v0.2.4 relationship surface
    per plan v3 §"Save flow".

    Pure function; does not write to disk.
    """
    status = result.get("status", "")
    session_id = result.get("session_id") or ""
    sid_short = session_id[:8] if session_id else "(unknown)"

    lines: List[str] = []
    lines.append(f"# Undeclared-Intent Spend — session `{sid_short}...`")
    lines.append("")
    lines.append(f"- Status: `{status}`")

    if status in (STATUS_NO_TOKEN_DATA, STATUS_NO_TURNS):
        if status == STATUS_NO_TOKEN_DATA:
            lines.append(
                "- No token-bearing `CONTEXT_SNAPSHOT` events were found "
                "in this session."
            )
            lines.append(
                "- This usually means Track 2 token-attribution hooks "
                "are not wired."
            )
        else:
            lines.append(
                "- Reasoning turns were detected but none carried "
                "populated token totals."
            )
        lines.append("")
        lines.append(_canonical_footer_markdown())
        return "\n".join(lines) + "\n"

    # ok / partial — full breakdown.
    total = int(result.get("total_tokens", 0) or 0)
    undeclared = int(result.get("undeclared_tokens", 0) or 0)
    declared = int(result.get("declared_tokens", 0) or 0)
    pct = float(result.get("undeclared_percent", 0.0) or 0.0)
    declared_pct = round(100.0 - pct, 1) if total > 0 else 0.0
    undeclared_turn_count = int(result.get("undeclared_turn_count", 0) or 0)
    total_turn_count = int(result.get("total_turn_count", 0) or 0)

    lines.append(f"- Total turns: {total_turn_count}")
    lines.append(f"- Undeclared turns: {undeclared_turn_count}")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append("| Metric | Tokens | Share |")
    lines.append("|---|---:|---:|")
    lines.append(f"| Total compute | {total:,} | 100.0% |")
    lines.append(f"| Undeclared | {undeclared:,} | {pct:.1f}% |")
    lines.append(f"| Declared | {declared:,} | {declared_pct:.1f}% |")
    lines.append("")

    undeclared_turns = result.get("undeclared_turns") or []
    if undeclared_turns:
        lines.append("## Undeclared turns")
        lines.append("")
        lines.append("| Turn | Tool(s) | Tokens | Reason(s) |")
        lines.append("|---|---|---:|---|")
        for entry in undeclared_turns:
            t_id = (entry.get("turn_id") or "")[:8]
            tools = ", ".join(entry.get("tool_ids") or []) or "—"
            tokens = int(entry.get("tokens", 0) or 0)
            reasons = ", ".join(entry.get("reasons") or [])
            lines.append(f"| `{t_id}...` | {tools} | {tokens:,} | {reasons} |")
        lines.append("")

    # v0.2.5 — Profile section. Only emitted when the trace
    # identifies a loaded operator-authored profile. Markdown report
    # omits both header and body when profile_loaded is not True, so
    # v0.2.4-shaped traces produce byte-identical reports.
    if result.get("profile_loaded") is True:
        fp = result.get("profile_fingerprint") or "(unknown)"
        psv = result.get("profile_schema_version")
        psv_str = str(psv) if isinstance(psv, int) else "(unknown)"
        lines.append("## Profile")
        lines.append("")
        lines.append("This session ran under a governance profile.")
        lines.append("")
        lines.append(f"- Profile fingerprint: `{fp}`")
        lines.append(f"- Schema version: {psv_str}")
        lines.append("")
        lines.append(
            "(For the full expectations that drove these results, see "
            "your local `~/.sentience/profile.yaml` — the analyzer does "
            "not embed profile contents in this report.)"
        )
        lines.append("")

    # v0.2.5 — High-consequence operations section. Always omitted
    # when no events present (regression guard).
    high_consequence_events = result.get("high_consequence_events") or []
    if high_consequence_events:
        lines.append("## High-consequence operations")
        lines.append("")
        lines.append("| Turn | Tool |")
        lines.append("|---|---|")
        for entry in high_consequence_events:
            t_id = (entry.get("turn_id") or "")[:8] if entry.get("turn_id") else "—"
            tool = entry.get("tool_id") or "—"
            lines.append(f"| `{t_id}` | {tool} |")
        lines.append("")

    # v0.2.5 — Task boundaries section.
    task_boundary_events = result.get("task_boundary_events") or []
    if task_boundary_events:
        lines.append("## Task boundaries crossed")
        lines.append("")
        lines.append("| Turn | Tool when crossed |")
        lines.append("|---|---|")
        for entry in task_boundary_events:
            t_id = (entry.get("turn_id") or "")[:8] if entry.get("turn_id") else "—"
            tool = entry.get("tool_id") or "—"
            lines.append(f"| `{t_id}` | {tool} |")
        lines.append("")

    if status == STATUS_PARTIAL:
        lines.append("## Notes")
        lines.append("")
        lines.append(
            "Status: `partial`. The analyzer accumulated trace warnings "
            "while reading this session. Numeric attribution above is "
            "correct for the readable subset; the full warnings list is "
            "available in the JSON output (`--json`)."
        )
        lines.append("")

    lines.append("## Operational interpretation")
    lines.append("")
    has_intent = bool(result.get("session_has_declared_intent", False))
    if has_intent:
        lines.append(
            f"{undeclared:,} tokens were attributed to turns that touched "
            "execution outside this session's declared operational intent. "
            "Were these valid and expected? If not, policy can intervene "
            "at the execution boundary — review, constraint, confirmation, "
            "or block."
        )
    else:
        lines.append(
            "This session contains no declared intent — every attributed "
            "turn is classified as undeclared. Often this reflects a "
            "surface-level limitation (e.g. Claude Code hooks today, which "
            "don't yet expose an intent-declaration primitive) rather than "
            "agent drift."
        )
    lines.append("")
    lines.append(_canonical_footer_markdown())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _format_turn_line(entry: Dict[str, Any]) -> str:
    """One CLI line for an undeclared turn."""
    t_id = (entry.get("turn_id") or "")[:8]
    tools = entry.get("tool_ids") or []
    primary_tool = tools[0] if tools else "—"
    tokens = int(entry.get("tokens", 0) or 0)
    reasons = entry.get("reasons") or []
    reason_str = ",".join(reasons)
    return (
        f"  Turn {t_id:<8}  {primary_tool:<24} "
        f"{tokens:>7,} tokens   {reason_str}"
    )


def _format_high_consequence_line(entry: Dict[str, Any]) -> str:
    """One CLI line for a HIGH_CONSEQUENCE_DETECTED event.

    Shape mirrors _format_turn_line for visual consistency:
    indented "Turn <short>  <tool>" so the three v0.2.5 sections
    align column-wise in screenshot output.
    """
    t_id = (entry.get("turn_id") or "")[:8] if entry.get("turn_id") else "—"
    tool = entry.get("tool_id") or "—"
    return f"  Turn {t_id:<8}  {tool}"


def _format_task_boundary_line(entry: Dict[str, Any]) -> str:
    """One CLI line for a TASK_BOUNDARY_CROSSED event."""
    t_id = (entry.get("turn_id") or "")[:8] if entry.get("turn_id") else "—"
    tool = entry.get("tool_id") or "—"
    return f"  Turn {t_id:<8}  {tool}"


def _render_no_token_data(sid_short: str) -> str:
    # FIX-1 (v0.2.8): lead with the live-session cause; short and crisp
    # so a chat surface renders it verbatim with nothing to "fill in".
    return (
        f"Undeclared-Intent Spend — session {sid_short}...\n"
        f"{'─' * 41}\n"
        "Status: no_token_data\n"
        "\n"
        "Per-turn token data is written when the Claude Code session\n"
        "ends. This session may still be running. End it, or run this\n"
        "against a previously ended session, then retry. If the\n"
        "session already ended, run `sentience init claude-code` and\n"
        "start a new session.\n"
    )


def _render_no_turns(sid_short: str) -> str:
    # FIX-1 (v0.2.8): same canonical live-session-first copy.
    return (
        f"Undeclared-Intent Spend — session {sid_short}...\n"
        f"{'─' * 41}\n"
        "Status: no_turns\n"
        "\n"
        "Per-turn token data is written when the Claude Code session\n"
        "ends. This session may still be running. End it, or run this\n"
        "against a previously ended session, then retry. If the\n"
        "session already ended, run `sentience init claude-code` and\n"
        "start a new session.\n"
    )


def _canonical_footer_markdown() -> str:
    """The canonical two-vector footer — LOCKED copy."""
    return (
        "---\n"
        "\n"
        "This is one session. A consolidated view across all your runs — "
        "drift trends, compute attributed across workflows, agent behavior "
        "compared over time — is what's next. If you'd find it useful, "
        "the most helpful thing is hearing what it should answer.\n"
        "\n"
        "Reply: <operators@crescerelabs.com>\n"
        "\n"
        "Or stay informed: <https://getsentience.ai/launch-list>\n"
    )


# ===========================================================================
# v0.2.6 CP2 — Policy-violation burn-rate renderers
# ===========================================================================
#
# Renderers consume the dict returned by
# `compute_policy_violation_burn_rate(events)` per plan v3.6 §"Policy-
# violation burn rate — output shape". CLI renderer uses `notes_short`
# (terse, 80-col-safe); Markdown renderer uses full `notes` (verbose,
# screen-real-estate-tolerant). Both renderers are pure: no I/O, no env
# reads, no input mutation.
#
# Attribution discipline: copy uses "appeared on turns representing" /
# "associated with" / "good place to inspect first" language. Never
# "reclaim", "save", "prevent", or "would have" wording — the metric
# is a prioritization signal, not a savings estimate.


def render_burn_rate_cli(
    result: Dict[str, Any], *, color: bool = True
) -> str:
    """Render a policy-violation burn-rate result for the terminal.

    Args:
        result: The dict returned by
            ``compute_policy_violation_burn_rate``.
        color: Reserved for future ANSI color support. Currently
            unused; the v0.2.6 surface is plain text for screenshot
            stability and PyPI README rendering.

    Returns:
        Plain-text rendering, terminated with a single newline.
        Status branches:

        * ``ok`` / ``partial`` — full breakdown + per-rule rows +
          non-additivity note (if multi-rule) + inspection-target
          line + footer.
        * ``no_violations`` — clean-session message; no breakdown,
          encouraging tone, no footer.
        * ``no_token_data`` — short notice pointing to ``--showcase``;
          no breakdown; no footer.
        * ``no_turns`` — short notice; no breakdown; no footer.

    CLI output fits within 80 columns at the widest row.

    Pure function; does not print the save prompt.
    """
    status = result.get("status", "")
    session_id = result.get("session_id") or ""
    sid_short = session_id[:8] if session_id else "(unknown)"

    if status == STATUS_NO_TOKEN_DATA:
        return _render_burn_rate_no_token_data(sid_short)

    if status == STATUS_NO_TURNS:
        return _render_burn_rate_no_turns(sid_short, result)

    if status == STATUS_NO_VIOLATIONS:
        return _render_burn_rate_no_violations(sid_short, result)

    # ok / partial — full breakdown.
    lines: List[str] = []
    title = f"Policy-Violation Burn Rate — session {sid_short}..."
    lines.append(title)
    lines.append("Compute associated with turns where policy rules fired.")
    lines.append("─" * max(len(title), 45))

    total = int(result.get("total_tokens", 0) or 0)
    firing_turns = int(result.get("violation_firing_turns", 0) or 0)
    # Clean turns = total turns minus violation-firing turns. We derive
    # total turn count from the by_rule sample_turn_ids + the firing
    # count, but a cleaner source is to count unique turns observed in
    # by_rule plus the clean turns visible from totals. Since the
    # result doesn't carry total_turn_count for burn-rate, infer clean
    # turns from violation_firing_turns vs the turn-count implied by
    # tokens. Conservative: report violation-firing-turn count
    # directly; clean turns line falls back to total_turns when known.
    # The analyzer does not currently surface total_turn_count for
    # burn-rate; we surface only violation_firing_turns.

    violation_associated = int(result.get("violation_associated_tokens", 0) or 0)
    lines.append(f"Total compute           {total:>9,} tokens")
    lines.append(f"Governance-attributable {violation_associated:>9,} tokens")
    lines.append(f"Violation-firing turns  {firing_turns:>9,}")

    lines.append("")

    # v0.2.6.1 D7 — total session burn vs governance-attributable burn.
    # Turns with no tool-call violation (reasoning / answer turns) still
    # carried real token burn; say so, so unattributed is never read as
    # "no burn."
    unattributed = total - violation_associated
    if unattributed > 0:
        lines.append(
            textwrap.fill(
                f"{unattributed:,} of those tokens are not tied to a "
                "tool-call violation (e.g. reasoning / answer turns) — real "
                "session burn, just not governance-attributable.",
                width=78,
            )
        )
        lines.append("")

    # v0.2.6.1 D5 — subagent burn exclusion disclosure.
    if result.get("subagent_activity_present") is True:
        lines.append(
            textwrap.fill(
                "Subagent (Task/Agent) token burn is excluded — these totals "
                "cover the main session transcript only.",
                width=78,
            )
        )
        lines.append("")

    by_rule = result.get("by_rule") or {}
    if by_rule:
        lines.append("By rule")
        for rule in sorted(by_rule.keys()):
            slot = by_rule[rule] or {}
            lines.append(_format_burn_rate_rule_line(rule, slot))
        lines.append("")

    # Non-additivity callout — sourced from `notes_short` for CLI
    # (terse). Fires whenever the analyzer determined more than one
    # rule has non-zero token_cost. Per plan v3.3, must surface inline
    # with the per-rule rows so operators see it at the moment they
    # look at the rule breakdown. Wrapped to 78 cols to honour the
    # 80-col CLI sanity gate (CP2).
    notes_short = result.get("notes_short") or []
    for note in notes_short:
        wrapped = textwrap.fill(note, width=78)
        lines.append(wrapped)
    if notes_short:
        lines.append("")

    # Primary inspection target — pick the rule with the highest
    # token_cost (ties broken by sorted rule string for determinism).
    inspection_target = _pick_primary_inspection_target(by_rule)
    if inspection_target is not None:
        rule, slot = inspection_target
        token_cost = int(slot.get("token_cost", 0) or 0)
        lines.append(
            f"{rule} appeared on turns representing ~{token_cost:,} tokens "
            "of session"
        )
        lines.append("compute. This is a good place to inspect first.")
        lines.append("")

    # Profile metadata footnote — only when the trace identifies a
    # loaded operator-authored profile.
    if result.get("profile_loaded") is True:
        fp = result.get("profile_fingerprint") or "(unknown)"
        psv = result.get("profile_schema_version")
        psv_str = str(psv) if isinstance(psv, int) else "(unknown)"
        lines.append(f"Profile: fingerprint {fp}, schema v{psv_str}")
        lines.append("")

    # Status partial — surface a small advisory.
    if status == STATUS_PARTIAL:
        warn_count = (
            int(result.get("unpaired_violation_count", 0) or 0)
            + int(result.get("untokened_pair_count", 0) or 0)
            + int(result.get("dedupe_conflict_count", 0) or 0)
            + int(result.get("malformed_event_count", 0) or 0)
            + int(result.get("unknown_rule_count", 0) or 0)
        )
        lines.append(
            f"Note: status=partial — {warn_count} trace warning(s) "
            "accumulated."
        )
        lines.append(
            "Numeric attribution above is correct for the readable "
            "subset; see"
        )
        lines.append("--json output for the warnings list.")
        lines.append("")

    # Strip trailing empty lines and re-add exactly one newline at end.
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def render_burn_rate_markdown(result: Dict[str, Any]) -> str:
    """Render a policy-violation burn-rate result as a Markdown report.

    Args:
        result: The dict returned by
            ``compute_policy_violation_burn_rate``.

    Returns:
        Markdown content suitable for writing to
        ``~/.sentience/reports/policy-violations-*.md``.

    Includes the canonical two-vector footer (operators@crescerelabs.com
    reply path + launch-list link).

    Pure function; does not write to disk.
    """
    status = result.get("status", "")
    session_id = result.get("session_id") or ""
    sid_short = session_id[:8] if session_id else "(unknown)"

    lines: List[str] = []
    lines.append(
        f"# Policy-Violation Burn Rate — session `{sid_short}...`"
    )
    lines.append("")
    lines.append(f"- Status: `{status}`")

    if status in (STATUS_NO_TOKEN_DATA, STATUS_NO_TURNS, STATUS_NO_VIOLATIONS):
        if status == STATUS_NO_TOKEN_DATA:
            # FIX-1 (v0.2.8): live-session cause first, wiring fallback.
            lines.append(
                "- Per-turn token data is written when the Claude Code "
                "session ends. This session may still be running."
            )
            lines.append(
                "- End it, or run this against a previously ended "
                "session, then retry. If the session already ended, run "
                "`sentience init claude-code` and start a new session."
            )
        elif status == STATUS_NO_TURNS:
            lines.append(
                "- Per-turn token data is written when the Claude Code "
                "session ends. This session may still be running."
            )
            lines.append(
                "- End it, or run this against a previously ended "
                "session, then retry. If the session already ended, run "
                "`sentience init claude-code` and start a new session."
            )
            # FIX-4 (v0.2.8): measured rule counts, attribution pending.
            by_rule = result.get("unpaired_by_rule") or {}
            if by_rule:
                lines.append(
                    "- Rules fired in this session (token attribution "
                    "pending session end): "
                    + ", ".join(f"{r} ×{n}" for r, n in by_rule.items())
                )
        else:  # STATUS_NO_VIOLATIONS
            total = int(result.get("total_tokens", 0) or 0)
            lines.append(
                f"- Total compute analysed: {total:,} tokens across the "
                "session's turns."
            )
            lines.append(
                "- No policy violations were recorded against the rules "
                "active in this session."
            )
            lines.append("")
            lines.append("## Interpretation")
            lines.append("")
            lines.append(
                "Your session was observable, your profile was loaded, "
                "and no policy violations were recorded against the rules "
                "active in this session. That is value: the run-to-run "
                "evidence record is intact and the rules you've declared "
                "did not fire."
            )
        lines.append("")
        lines.append(_canonical_footer_markdown())
        return "\n".join(lines) + "\n"

    # ok / partial — full breakdown.
    total = int(result.get("total_tokens", 0) or 0)
    violation_associated = int(
        result.get("violation_associated_tokens", 0) or 0
    )
    firing_turns = int(result.get("violation_firing_turns", 0) or 0)

    lines.append(f"- Total compute: {total:,} tokens")
    lines.append(
        f"- Violation-associated compute: {violation_associated:,} tokens "
        f"(across {firing_turns} unique violation-firing turn(s))"
    )
    # v0.2.6.1 D7 — disclose the unattributed remainder as real burn.
    unattributed = total - violation_associated
    if unattributed > 0:
        lines.append(
            f"- Not governance-attributable: {unattributed:,} tokens "
            "(reasoning / answer turns with no tool-call violation — real "
            "session burn)"
        )
    # v0.2.6.1 D5 — subagent burn exclusion.
    if result.get("subagent_activity_present") is True:
        lines.append(
            "- Subagent (Task/Agent) token burn is **excluded**; these totals "
            "cover the main session transcript only."
        )
    lines.append("")

    by_rule = result.get("by_rule") or {}
    if by_rule:
        lines.append("## By rule")
        lines.append("")
        lines.append("| Rule | Turns | Tokens | Description |")
        lines.append("|---|---:|---:|---|")
        for rule in sorted(by_rule.keys()):
            slot = by_rule[rule] or {}
            turn_count = int(slot.get("turn_count", 0) or 0)
            token_cost = int(slot.get("token_cost", 0) or 0)
            desc = slot.get("description") or ""
            lines.append(
                f"| `{rule}` | {turn_count} | {token_cost:,} | {desc} |"
            )
        lines.append("")

    # Non-additivity note — full version for Markdown. Surfaces inline
    # below the by-rule table per plan v3.3 placement rule.
    notes = result.get("notes") or []
    if notes:
        lines.append("> **Note on by-rule totals.** " + " ".join(notes))
        lines.append("")

    # Sample turns per rule — operator's click-through anchor into the
    # underlying trace. Markdown gets the full per-rule sample list;
    # CLI showed only the inspection-target line.
    has_samples = any(
        (by_rule.get(r, {}).get("sample_turn_ids") or [])
        for r in by_rule
    )
    if has_samples:
        lines.append("## Sample turns per rule")
        lines.append("")
        lines.append("| Rule | Sample turn IDs |")
        lines.append("|---|---|")
        for rule in sorted(by_rule.keys()):
            slot = by_rule[rule] or {}
            samples = slot.get("sample_turn_ids") or []
            if not samples:
                continue
            sample_str = ", ".join(f"`{s[:8]}...`" for s in samples)
            lines.append(f"| `{rule}` | {sample_str} |")
        lines.append("")

    # Profile section — only emitted when the trace identifies a loaded
    # operator-authored profile (parity with undeclared-intent renderer).
    if result.get("profile_loaded") is True:
        fp = result.get("profile_fingerprint") or "(unknown)"
        psv = result.get("profile_schema_version")
        psv_str = str(psv) if isinstance(psv, int) else "(unknown)"
        lines.append("## Profile")
        lines.append("")
        lines.append("This session ran under a governance profile.")
        lines.append("")
        lines.append(f"- Profile fingerprint: `{fp}`")
        lines.append(f"- Schema version: {psv_str}")
        lines.append("")
        lines.append(
            "(For the full expectations that drove these results, see "
            "your local `~/.sentience/profile.yaml` — the analyzer does "
            "not embed profile contents in this report.)"
        )
        lines.append("")

    # Status partial — surface a Notes section.
    if status == STATUS_PARTIAL:
        lines.append("## Notes")
        lines.append("")
        lines.append(
            "Status: `partial`. The analyzer accumulated trace warnings "
            "while reading this session. Numeric attribution above is "
            "correct for the readable subset; the full warnings list is "
            "available in the JSON output (`--json`)."
        )
        lines.append("")

    # Operational interpretation — uses association language per
    # attribution discipline (no savings/causality wording).
    lines.append("## Operational interpretation")
    lines.append("")
    inspection_target = _pick_primary_inspection_target(by_rule)
    if inspection_target is not None:
        rule, slot = inspection_target
        token_cost = int(slot.get("token_cost", 0) or 0)
        lines.append(
            f"`{rule}` appeared on turns representing ~{token_cost:,} "
            "tokens of session compute. This is a good place to "
            "inspect first."
        )
    else:
        # ok status with empty by_rule should not happen (would be
        # no_violations instead), but defensive.
        lines.append(
            f"{violation_associated:,} tokens of session compute were "
            "associated with turns where policy rules fired."
        )
    lines.append("")

    lines.append(_canonical_footer_markdown())
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Burn-rate internal helpers
# ---------------------------------------------------------------------------


def _format_burn_rate_rule_line(rule: str, slot: Dict[str, Any]) -> str:
    """One CLI line for a per-rule row.

    Format chosen to fit within 80 columns even at maximum field widths
    (rule string is fixed-width 7 chars; turn count and token count get
    right-aligned columns; description is the trailing variable-width
    field, truncated if needed).
    """
    turn_count = int(slot.get("turn_count", 0) or 0)
    token_cost = int(slot.get("token_cost", 0) or 0)
    desc = slot.get("description") or ""
    turn_str = f"{turn_count} turn{'s' if turn_count != 1 else ''}"
    # Truncate description so the full line stays under ~80 cols.
    # Header indent "  " (2) + rule (7) + 4 spaces + turn_str (~8) +
    # 4 spaces + token_str (~12) + 3 spaces = ~40 fixed; description
    # gets ~38 chars. Truncate at 38 with ellipsis.
    if len(desc) > 38:
        desc = desc[:35] + "..."
    return (
        f"  {rule:<7}  {turn_str:<8}  {token_cost:>9,} tokens   {desc}"
    )


def _pick_primary_inspection_target(
    by_rule: Dict[str, Dict[str, Any]],
) -> Optional[tuple]:
    """Pick the rule with the highest token_cost as the primary
    inspection target.

    Ties broken by sorted rule string for deterministic output. Returns
    ``None`` if by_rule is empty.
    """
    if not by_rule:
        return None
    candidates = [
        (rule, slot)
        for rule, slot in by_rule.items()
        if int((slot or {}).get("token_cost", 0) or 0) > 0
    ]
    if not candidates:
        return None
    # Sort by token_cost descending, then by rule string ascending for
    # deterministic tie-breaking.
    candidates.sort(key=lambda kv: (-int(kv[1].get("token_cost", 0) or 0), kv[0]))
    return candidates[0]


def _render_burn_rate_no_token_data(sid_short: str) -> str:
    # FIX-1 (v0.2.8): the old text led with "may not be wired", which
    # seeded a false diagnosis in the live clean-room (findings F6/F13).
    # Live-session cause first; wiring is the fallback, not the lead.
    return (
        f"Policy-Violation Burn Rate — session {sid_short}...\n"
        f"{'─' * 45}\n"
        "Status: no_token_data\n"
        "\n"
        "Per-turn token data is written when the Claude Code session\n"
        "ends. This session may still be running. End it, or run this\n"
        "against a previously ended session, then retry. If the\n"
        "session already ended, run `sentience init claude-code` and\n"
        "start a new session.\n"
    )


def _render_burn_rate_no_turns(sid_short: str, result: Dict[str, Any]) -> str:
    # FIX-1 (v0.2.8): canonical live-session-first copy.
    # FIX-4 (v0.2.8): rule counts are MEASURED data the analyzer already
    # collected — report them even though token attribution is pending,
    # so a turn-less session never reads as "nothing to report".
    out = (
        f"Policy-Violation Burn Rate — session {sid_short}...\n"
        f"{'─' * 45}\n"
        "Status: no_turns\n"
        "\n"
        "Per-turn token data is written when the Claude Code session\n"
        "ends. This session may still be running. End it, or run this\n"
        "against a previously ended session, then retry. If the\n"
        "session already ended, run `sentience init claude-code` and\n"
        "start a new session.\n"
    )
    by_rule = result.get("unpaired_by_rule") or {}
    if by_rule:
        out += (
            "\n"
            "Rules fired in this session (token attribution pending "
            "session end):\n"
        )
        out += (
            "    "
            + "    ".join(f"{rule}  {n}" for rule, n in by_rule.items())
            + "\n"
        )
    return out


def _render_burn_rate_no_violations(
    sid_short: str, result: Dict[str, Any]
) -> str:
    """Clean-session output. Encouraging, never empty.

    Per plan v3.1 wording discipline: avoids overclaiming. We say "no
    policy violations were recorded against the rules active in this
    session" — NOT "the agent stayed within all meaningful boundaries"
    (which would overclaim when the operator's profile is permissive).
    """
    total = int(result.get("total_tokens", 0) or 0)
    lines = [
        f"Policy-Violation Burn Rate — session {sid_short}...",
        "─" * 45,
        "Status: no_violations",
        "",
        f"Total compute analysed: {total:,} tokens.",
        "",
        "No policy violations were recorded against the rules active",
        "in this session. Your session was observable, your profile",
        "was loaded, and the rules you've declared did not fire.",
        "",
        "That is value: the run-to-run evidence record is intact.",
    ]
    return "\n".join(lines) + "\n"


# ===========================================================================
# v0.2.6 CP5 — Pulse renderers
# ===========================================================================
#
# Renderers consume the dict returned by
# `compute_pulse(events)` per plan v3.6 §"Pulse — output shape". CLI
# renderer is screenshot-worthy and 80-col-safe; Markdown renderer is
# shareable / paste-into-an-issue.
#
# Renderer purity discipline (per plan v3.6 §CP5):
#
# * Renderers read the result dict and produce text. They NEVER read
#   sync state from disk, NEVER read environment variables, NEVER open
#   files. All eligibility decisions (sync prompt show/hide, decline-
#   cooldown logic) live in the CP6 CLI handler and are encoded in the
#   result dict (`result.sync_prompt.show`) before the renderer is
#   called.
# * The sync-prompt *text* lives here so v0.3.x can update it
#   without touching CLI code. The sync-prompt *eligibility* lives in
#   the CLI handler (single source of truth for disk/env interactions).

# Email-list footer text. Source-of-truth lives here so a copy
# refresh in v0.3.x only touches one file. Eligibility (`show`) is
# decided by the CP6 CLI handler.
_PULSE_SYNC_FOOTER_HEADING = (
    "Want this pulse delivered weekly via email?"
)
_PULSE_SYNC_FOOTER_LINK = (
    "  Join the list: https://getsentience.ai/sentience-sync"
)

# Pulse status constants (mirrored from compute_pulse so renderer
# remains independently importable — same pattern used by the analyzer
# status constants at the top of this module).
PULSE_STATUS_OK = "ok"
PULSE_STATUS_PARTIAL = "partial"
PULSE_STATUS_LIMITED = "limited"
PULSE_STATUS_NO_SIGNAL = "no_signal"

# Mandatory clean-session Interpretation block copy. Locked per plan
# v3.1 wording discipline — uses the qualified "no policy violations
# were recorded against the rules active in this session" phrasing
# (does NOT overclaim that the agent stayed within all meaningful
# boundaries when the operator's profile is permissive).
_CLEAN_SESSION_INTERPRETATION = (
    "Your session was observable, your profile was loaded, and no policy\n"
    "violations were recorded against the rules active in this session.\n"
    "That is still value. Pulse will surface more signal as your profile\n"
    "tightens or agent behavior shifts. Run `sentience pulse` after each\n"
    "session to track changes over time."
)


def _format_duration(seconds: int) -> str:
    """Render an integer second-count as ``Hh Mm Ss`` / ``Mm Ss`` /
    ``Ns``. Pure helper used by both pulse renderers so the duration
    string is consistent across CLI and Markdown surfaces.
    """
    s = max(0, int(seconds))
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _count_usable_sub_analyzers(result: Dict[str, Any]) -> tuple:
    """Return ``(usable, total)`` — how many of the pulse's sub-
    analyzers produced usable signal (status in ``ok`` /
    ``no_violations``) out of the total composed in.

    Used by the ``limited`` status renderer to produce the specific
    "N of M analyzers had usable data" notice mandated by plan v3.6
    §CP5.
    """
    sub_dicts = [
        result.get("undeclared_intent") or {},
        result.get("policy_violations_burn_rate") or {},
    ]
    usable = 0
    for sub in sub_dicts:
        status = sub.get("status")
        if status in (STATUS_OK, STATUS_NO_VIOLATIONS):
            usable += 1
    return usable, len(sub_dicts)


def _render_pulse_undeclared_intent_section(
    sub: Dict[str, Any],
) -> List[str]:
    """One pulse section: undeclared-intent. Always ends with a
    'Why it matters' line per the plan's 60-second-value mandate.
    """
    lines = ["Undeclared-intent spend"]
    status = sub.get("status", "")
    total = int(sub.get("total_tokens", 0) or 0)
    undeclared = int(sub.get("undeclared_tokens", 0) or 0)
    declared = int(sub.get("declared_tokens", 0) or 0)
    pct = float(sub.get("undeclared_percent", 0.0) or 0.0)
    has_intent = bool(sub.get("session_has_declared_intent", False))

    if status == STATUS_NO_TOKEN_DATA:
        lines.append(
            "  No per-turn token data was available for this session."
        )
        lines.append(
            "  Why it matters: this often reflects a surface-bound limitation"
        )
        lines.append(
            "  (e.g. Claude Code hooks today), not agent drift."
        )
        lines.extend(_pulse_tool_calls_lines(sub))
        return lines

    if status == STATUS_NO_TURNS:
        lines.append(
            "  No reasoning turns with CONTEXT_SNAPSHOT identifiers were found."
        )
        lines.append(
            "  Why it matters: per-turn attribution requires turn-identified"
        )
        lines.append("  context snapshots — none were present.")
        lines.extend(_pulse_tool_calls_lines(sub))
        return lines

    # Branch on the actual undeclared/total distribution, not on
    # has_intent in isolation — has_intent==True only means at least
    # one INTENT_DECLARED event existed, not that every turn was
    # attributable. The four cases:
    if total > 0 and undeclared == total and not has_intent:
        # Surface-bound: NO intent declared anywhere AND every turn
        # is undeclared. This is the Claude Code-today shape.
        lines.append(
            f"  {undeclared:,} of {total:,} tokens were unattributed — "
            "no declared intent was"
        )
        lines.append("  present in this session.")
        lines.append(
            "  Why it matters: often a surface-bound limitation (e.g. "
            "Claude Code today),"
        )
        lines.append("  not agent drift.")
    elif undeclared > 0:
        # Mixed: some turns are attributed, some aren't. The gap is
        # what the operator can act on. Copy is identical whether
        # has_intent is True (intent declared on some turns) or
        # False (no global intent but some turns escaped classification).
        lines.append(
            f"  {undeclared:,} of {total:,} tokens were attached to turns "
            f"without declared intent."
        )
        lines.append(
            "  Why it matters: this is where agent work became harder "
            "to attribute."
        )
    elif total > 0:
        # Clean / fully-attributed session — undeclared==0.
        lines.append(
            f"  {declared:,} of {total:,} tokens were attached to turns "
            f"with declared intent"
        )
        if pct == 0.0:
            lines.append("  (100%).")
        else:
            lines.append(f"  ({100.0 - pct:.1f}%).")
        lines.append(
            "  Why it matters: every turn was attributable to your "
            "declared session intent."
        )
    else:
        # Defensive fallback: total==0 (no tokens at all). Earlier
        # status-branch guards (no_token_data) typically catch this;
        # this branch covers any analytic edge case where status is
        # ok/partial but token totals are zero.
        lines.append("  No per-turn token attribution available.")
        lines.append(
            "  Why it matters: the trace had reasoning turns but no "
            "populated tokens — review the trace shape."
        )

    # IR-4 (v0.2.8.1): surface the four token classes — the most
    # interesting fact about a Claude Code session (cache reads dominate),
    # otherwise only reachable by summing raw fields. The four sum to total.
    # IR-2 (v0.2.8.1): state the dedupe semantics so the reader does not
    # misread cache-read totals as cumulative.
    bd = _pulse_token_breakdown_lines(sub, total)
    if bd:
        lines.extend(bd)
    lines.extend(_pulse_tool_calls_lines(sub))
    lines.extend(_pulse_tool_token_attr_lines(sub))
    return lines


def _pulse_token_breakdown_lines(
    sub: Dict[str, Any], total: int,
) -> List[str]:
    """IR-4 + IR-2: the four-class breakdown + dedupe methodology line.

    Shared by the CLI and Markdown pulse renderers so both surfaces stay
    consistent. Returns [] when there is no token breakdown to show.
    """
    breakdown = sub.get("token_breakdown") or {}
    if total <= 0 or not breakdown:
        return []
    cr = int(breakdown.get("cached_read", 0) or 0)
    cw = int(breakdown.get("cached_write", 0) or 0)
    pr = int(breakdown.get("prompt", 0) or 0)
    co = int(breakdown.get("completion", 0) or 0)
    cr_pct = (cr / total * 100.0) if total > 0 else 0.0
    return [
        "  Token classes (sum to total compute):",
        f"    cached read   {cr:,} ({cr_pct:.0f}%)",
        f"    cached write  {cw:,}",
        f"    prompt        {pr:,}",
        f"    completion    {co:,}",
        "  Per-turn usage is deduped by requestId.",
    ]


def _pulse_tool_calls_lines(sub: Dict[str, Any]) -> List[str]:
    """F21 (v0.2.9): tool-call counts as a first-class pulse field —
    total, the four operation classes (mirroring the IR-4 token-class
    breakdown shape), and the top tools by call count.

    Shared by the CLI and Markdown pulse renderers. Independent of token
    data: returns counts whenever any tool call was observed (so a
    no_token_data session still surfaces what the agent did). Returns []
    when no tool calls were observed.
    """
    tc = sub.get("tool_calls") or {}
    total = int(tc.get("total", 0) or 0)
    if total <= 0:
        return []
    by_op = tc.get("by_operation") or {}
    ex = int(by_op.get("execute", 0) or 0)
    rd = int(by_op.get("read", 0) or 0)
    wr = int(by_op.get("write", 0) or 0)
    de = int(by_op.get("delete", 0) or 0)
    lines = [
        f"  Tool calls ({total:,} total):",
        f"    execute       {ex:,}",
        f"    read          {rd:,}",
        f"    write         {wr:,}",
        f"    delete        {de:,}",
    ]
    by_tool = tc.get("by_tool") or {}
    if by_tool:
        top = sorted(by_tool.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:5]
        top_str = ", ".join(f"{name} ({int(n):,})" for name, n in top)
        lines.append(f"  Top tools by call count: {top_str}")
    return lines


def _pulse_tool_calls_md_lines(sub: Dict[str, Any]) -> List[str]:
    """F21 (v0.2.9): tool-call counts for the Markdown pulse — a small
    table (four operation classes + total) plus a top-tools line. Mirrors
    ``_pulse_tool_calls_lines`` for the CLI surface. Returns [] when no
    tool calls were observed.
    """
    tc = sub.get("tool_calls") or {}
    total = int(tc.get("total", 0) or 0)
    if total <= 0:
        return []
    by_op = tc.get("by_operation") or {}
    ex = int(by_op.get("execute", 0) or 0)
    rd = int(by_op.get("read", 0) or 0)
    wr = int(by_op.get("write", 0) or 0)
    de = int(by_op.get("delete", 0) or 0)
    lines = [
        "### Tool calls",
        "",
        "| Operation | Calls |",
        "|---|---:|",
        f"| Execute | {ex:,} |",
        f"| Read | {rd:,} |",
        f"| Write | {wr:,} |",
        f"| Delete | {de:,} |",
        f"| **Total** | **{total:,}** |",
        "",
    ]
    by_tool = tc.get("by_tool") or {}
    if by_tool:
        top = sorted(by_tool.items(), key=lambda kv: (-int(kv[1]), kv[0]))[:5]
        top_str = ", ".join(f"{name} ({int(n):,})" for name, n in top)
        lines.append(f"_Top tools by call count: {top_str}._")
    return lines


def _pulse_tool_token_attr_lines(sub: Dict[str, Any]) -> List[str]:
    """IR-3 (v0.2.9): measured tool-token attribution for the CLI pulse.

    A1 (headline): tokens on turns that fired >=1 tool call.
    A2 (secondary): per-tool full-turn-credit, NON-ADDITIVE.

    Wording guard: every figure is turn-attributed ("tokens on turns
    involving tool X"), never per-tool spend — the model meters tokens per
    turn, not per tool. Returns [] when there is no token attribution.
    """
    attr = sub.get("tool_token_attribution") or {}
    total = int(attr.get("total_tokens", 0) or 0)
    if total <= 0:
        return []
    a1 = int(attr.get("tokens_on_turns_with_tool_calls", 0) or 0)
    pct = float(attr.get("percent_of_total", 0.0) or 0.0)
    lines = [
        f"  Tokens on turns that fired ≥1 tool call: {a1:,} "
        f"({pct:.1f}% of total)",
    ]
    by_tool = attr.get("by_tool") or []
    if by_tool:
        lines.append(
            "  Tokens on turns involving each tool "
            "(full-turn-credit; non-additive):"
        )
        for e in by_tool[:5]:
            tid = e.get("tool_id", "")
            tk = int(e.get("tokens", 0) or 0)
            tn = int(e.get("turn_count", 0) or 0)
            lines.append(f"    {tid}: {tk:,} on {tn} turns")
        lines.append(
            "  Non-additive: turns with several tools credit each the "
            "full turn total."
        )
    return lines


def _pulse_tool_token_attr_md_lines(sub: Dict[str, Any]) -> List[str]:
    """IR-3 (v0.2.9): measured tool-token attribution for the Markdown pulse.
    Mirrors ``_pulse_tool_token_attr_lines``. Returns [] when there is no
    token attribution."""
    attr = sub.get("tool_token_attribution") or {}
    total = int(attr.get("total_tokens", 0) or 0)
    if total <= 0:
        return []
    a1 = int(attr.get("tokens_on_turns_with_tool_calls", 0) or 0)
    pct = float(attr.get("percent_of_total", 0.0) or 0.0)
    lines = [
        "### Tool-token attribution",
        "",
        f"**Tokens on turns that fired ≥1 tool call:** {a1:,} "
        f"({pct:.1f}% of total)",
        "",
    ]
    by_tool = attr.get("by_tool") or []
    if by_tool:
        lines.append(
            "Tokens on turns involving each tool "
            "(full-turn-credit, non-additive):"
        )
        lines.append("")
        lines.append("| Tool | Tokens on its turns | Turns |")
        lines.append("|---|---:|---:|")
        for e in by_tool[:10]:
            tid = e.get("tool_id", "")
            tk = int(e.get("tokens", 0) or 0)
            tn = int(e.get("turn_count", 0) or 0)
            lines.append(f"| {tid} | {tk:,} | {tn} |")
        lines.append("")
        lines.append(
            "_Non-additive: a turn involving several tools credits each "
            "with the full turn total._"
        )
    return lines


def _render_pulse_burn_rate_section(
    sub: Dict[str, Any],
) -> List[str]:
    """One pulse section: policy-violation burn rate. Always ends
    with a 'Why it matters' line.
    """
    lines = ["Policy-violation burn rate"]
    status = sub.get("status", "")

    if status == STATUS_NO_TOKEN_DATA:
        lines.append(
            "  No per-turn token data — burn-rate attribution unavailable."
        )
        lines.append(
            "  Why it matters: this is a surface-bound limitation, "
            "not a clean session."
        )
        return lines

    if status == STATUS_NO_TURNS:
        lines.append(
            "  No reasoning turns were detected in this session."
        )
        lines.append(
            "  Why it matters: per-turn attribution requires turn-identified"
        )
        lines.append("  context snapshots — none were present.")
        return lines

    if status == STATUS_NO_VIOLATIONS:
        # Per plan v3.6 — explicit "no violations" section + why-it-matters.
        # The fuller Interpretation block is appended once at the bottom
        # of the pulse, not duplicated here.
        lines.append("  No policy violations recorded.")
        lines.append(
            "  Why it matters: your profile rules did not fire in "
            "this session."
        )
        return lines

    # ok / partial — full per-rule breakdown.
    lines.append(
        "  Compute associated with turns where policy rules fired."
    )
    firing_turns = int(sub.get("violation_firing_turns", 0) or 0)
    assoc_tokens = int(sub.get("violation_associated_tokens", 0) or 0)
    turns_word = "turn" if firing_turns == 1 else "turns"
    lines.append(
        f"  {firing_turns} violation-firing {turns_word} · "
        f"{assoc_tokens:,} tokens"
    )
    # v0.2.6.1 D7 — flag the unattributed remainder as real burn (terse).
    total_burn = int(sub.get("total_tokens", 0) or 0)
    unattributed = total_burn - assoc_tokens
    if unattributed > 0:
        lines.append(
            f"  +{unattributed:,} tokens on turns with no tool-call violation."
        )

    by_rule = sub.get("by_rule") or {}
    for rule in sorted(by_rule.keys()):
        slot = by_rule[rule] or {}
        tc = int(slot.get("token_cost", 0) or 0)
        turns = int(slot.get("turn_count", 0) or 0)
        desc = slot.get("description") or ""
        turns_str = f"{turns} turn" if turns == 1 else f"{turns} turns"
        prefix = f"    {rule}  {turns_str}  {tc:,} tokens   "
        # Trim description so the full row stays within the 80-col
        # CLI sanity gate (plan v3.6 §CP5). textwrap.shorten preserves
        # word boundaries and appends an ellipsis when truncation
        # happens.
        budget = max(10, 80 - len(prefix))
        if desc:
            desc_short = textwrap.shorten(
                desc, width=budget, placeholder="…"
            )
        else:
            desc_short = ""
        lines.append(prefix + desc_short)

    # Non-additivity note from notes_short (terse, CLI-suitable).
    notes_short = sub.get("notes_short") or []
    for note in notes_short:
        for wrap_line in textwrap.fill(
            note, width=76, initial_indent="  ", subsequent_indent="  "
        ).splitlines():
            lines.append(wrap_line)

    # v0.2.6.1 D5 — subagent exclusion disclosure (terse for pulse).
    if sub.get("subagent_activity_present") is True:
        lines.append(
            "  Subagent (Task/Agent) burn excluded — main session only."
        )

    # Why-it-matters — point at the highest-token-cost rule.
    if by_rule:
        primary_rule, primary_slot = max(
            sorted(by_rule.items()),  # sorted for deterministic tiebreak
            key=lambda kv: int((kv[1] or {}).get("token_cost", 0) or 0),
        )
        primary_tc = int((primary_slot or {}).get("token_cost", 0) or 0)
        lines.append(
            f"  Why it matters: {primary_rule} appeared on turns "
            f"representing {primary_tc:,} tokens."
        )
        lines.append(
            "  This is the first rule to inspect if you want tighter "
            "agent boundaries."
        )
    return lines


def _render_pulse_advisory_section(
    summary: Dict[str, Any],
) -> List[str]:
    """One pulse section: advisory flags. Lists only flags with
    non-zero counts; emits a single-line zero-state message when all
    counts are zero. Always ends with a 'Why it matters' line per
    the v3 60-second-value mandate.
    """
    lines = ["Advisory flags"]
    items = [(k, int(v or 0)) for k, v in summary.items()]
    nonzero = [(k, v) for k, v in items if v > 0]
    if not nonzero:
        lines.append("  None fired in this session.")
        lines.append(
            "  Why it matters: no profile-driven advisory thresholds "
            "were crossed."
        )
        return lines

    # Preserve a stable display order matching the advisory-flag
    # constant order in the analyzer (alphabetical is also fine for
    # determinism). We sort by flag name for stability.
    for flag, count in sorted(nonzero):
        lines.append(f"  {flag}: {count}")
    # Why-it-matters — surface the two flags operators most care about
    # when present.
    boundary = summary.get("TASK_BOUNDARY_CROSSED", 0) or 0
    high = summary.get("HIGH_CONSEQUENCE_DETECTED", 0) or 0
    if boundary or high:
        parts = []
        if boundary:
            parts.append(
                f"the agent crossed into new tasks {boundary} time"
                f"{'s' if boundary != 1 else ''} this session"
            )
        if high:
            parts.append(
                f"triggered {high} high-consequence operation"
                f"{'s' if high != 1 else ''}"
            )
        lines.append("  Why it matters: " + " and ".join(parts) + ".")
        lines.append("  Review the trace for context.")
    else:
        lines.append(
            "  Why it matters: review the trace for any of the above "
            "advisory signals."
        )
    return lines


def _render_pulse_profile_line(result: Dict[str, Any]) -> Optional[str]:
    """Optional header line: ``Profile: <fingerprint> (schema vN,
    loaded)``. Returns ``None`` if no profile metadata is present so
    the renderer can omit the line cleanly.
    """
    if result.get("profile_loaded") is not True:
        return None
    fp = result.get("profile_fingerprint") or "(unknown)"
    psv = result.get("profile_schema_version")
    psv_str = str(psv) if isinstance(psv, int) else "(unknown)"
    return f"Profile: {fp} (schema v{psv_str}, loaded)"


def _render_pulse_sync_footer_lines(
    result: Dict[str, Any],
) -> List[str]:
    """Render the email-list footer iff
    ``result.sync_prompt.show`` is truthy. Returns an empty list
    otherwise (no separator, no blank line — the renderer at the
    call site decides whether to emit a leading divider).
    """
    sync_prompt = result.get("sync_prompt") or {}
    if not sync_prompt.get("show"):
        return []
    return [
        "─" * 40,
        _PULSE_SYNC_FOOTER_HEADING,
        _PULSE_SYNC_FOOTER_LINK,
    ]


def render_pulse_cli(
    result: Dict[str, Any], *, color: bool = True
) -> str:
    """Render a pulse result for the terminal.

    Args:
        result: The dict returned by
            :func:`sentience_governor.analyze.pulse.compute_pulse`.
        color: Reserved for future ANSI color support. Currently
            unused; the v0.2.6 pulse surface is plain text for
            screenshot stability and PyPI README rendering.

    Returns:
        Plain-text rendering, terminated with a single newline.

    Pulse status branches (see plan v3.6 §"Pulse status merge
    rules"):

    * ``ok`` — every section rendered with full breakdown.
    * ``partial`` — same shape as ``ok`` plus a status notice.
    * ``limited`` — prepends "Limited signal — N of M analyzers had
      usable data" notice per the spec.
    * ``no_signal`` — terse, honest "no usable analyzer signal"
      framing.

    Email-list footer shows iff
    ``result.sync_prompt.show`` is truthy. The footer text lives in
    this module; eligibility is decided by the CP6 CLI handler.

    Pure function; does not read disk, env vars, or files; does
    not print the save prompt.
    """
    session_id = result.get("session_id") or ""
    sid_short = session_id[:8] if session_id else "(unknown)"
    status = result.get("status", "")

    lines: List[str] = []
    title = f"Sentience Pulse — session {sid_short}..."
    lines.append(title)
    lines.append("═" * max(len(title), 37))

    # Profile header line — omitted when no profile metadata.
    profile_line = _render_pulse_profile_line(result)
    if profile_line:
        lines.append(profile_line)

    # Session-summary header line.
    summary = result.get("session_summary") or {}
    total_events = int(summary.get("total_events", 0) or 0)
    total_turns = int(summary.get("total_turns", 0) or 0)
    duration_s = int(summary.get("session_duration_seconds", 0) or 0)
    lines.append(
        f"Total events: {total_events}   "
        f"Total turns: {total_turns}   "
        f"Duration: {_format_duration(duration_s)}"
    )
    lines.append("")

    # Limited-signal banner — specific call-out per plan v3.6.
    if status == PULSE_STATUS_LIMITED:
        usable, total = _count_usable_sub_analyzers(result)
        lines.append(
            f"Limited signal — {usable} of {total} analyzers had usable data."
        )
        lines.append("")

    if status == PULSE_STATUS_NO_SIGNAL:
        # Terse, honest framing — no per-section breakdown to show.
        # FIX-1 (v0.2.8): canonical live-session-first explanation, so a
        # chat surface can render this verbatim with nothing to add.
        lines.append("No usable analyzer signal in this session.")
        lines.append("")
        lines.append(
            "Per-turn token data is written when the Claude Code "
            "session ends."
        )
        lines.append(
            "This session may still be running. End it, or run this "
            "against a"
        )
        lines.append(
            "previously ended session, then retry. If the session "
            "already ended,"
        )
        lines.append(
            "run `sentience init claude-code` and start a new session."
        )
        lines.append("")
        footer = _render_pulse_sync_footer_lines(result)
        if footer:
            lines.extend(footer)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines) + "\n"

    # Section: undeclared-intent.
    lines.extend(
        _render_pulse_undeclared_intent_section(
            result.get("undeclared_intent") or {}
        )
    )
    lines.append("")

    # Section: burn rate. Per plan v3.6, the burn-rate section heading
    # is "Policy-violation burn rate" when ok/partial, "Policy violations"
    # when no_violations (matches the §"CLI output — clean session"
    # sketch). We keep a single heading "Policy-violation burn rate"
    # for consistency across statuses — the no_violations path body copy
    # ("No policy violations recorded") already makes the state clear.
    burn = result.get("policy_violations_burn_rate") or {}
    lines.extend(_render_pulse_burn_rate_section(burn))
    lines.append("")

    # Section: advisory flags.
    lines.extend(
        _render_pulse_advisory_section(
            result.get("advisory_flag_summary") or {}
        )
    )
    lines.append("")

    # Interpretation block — MANDATORY for the clean-session path
    # per plan v3.6 §"Clean-session output discipline". A clean
    # session = burn-rate status no_violations.
    if burn.get("status") == STATUS_NO_VIOLATIONS:
        lines.append("Interpretation")
        for line in _CLEAN_SESSION_INTERPRETATION.splitlines():
            lines.append(f"  {line}")
        lines.append("")

    # Partial status — small advisory.
    if status == PULSE_STATUS_PARTIAL:
        lines.append(
            "Note: status=partial — at least one analyzer produced "
            "warnings."
        )
        lines.append(
            "See --json output for the full warnings list."
        )
        lines.append("")

    # Email-list footer (eligibility comes from CP6 handler).
    footer = _render_pulse_sync_footer_lines(result)
    if footer:
        lines.extend(footer)

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def render_pulse_markdown(result: Dict[str, Any]) -> str:
    """Render a pulse result as a Markdown report suitable for
    ``~/.sentience/reports/pulse-<sid>-<ts>.md``.

    Includes the same five sections as the CLI surface in the same
    order — header, undeclared-intent, burn rate, advisory flags,
    Interpretation block (when clean) — plus the canonical
    email-list footer iff ``result.sync_prompt.show`` is truthy.

    Pure function; does not write to disk.
    """
    session_id = result.get("session_id") or ""
    sid_short = session_id[:8] if session_id else "(unknown)"
    status = result.get("status", "")

    lines: List[str] = []
    lines.append(f"# Sentience Pulse — session `{sid_short}...`")
    lines.append("")
    lines.append(f"- Status: `{status}`")

    summary = result.get("session_summary") or {}
    total_events = int(summary.get("total_events", 0) or 0)
    total_turns = int(summary.get("total_turns", 0) or 0)
    duration_s = int(summary.get("session_duration_seconds", 0) or 0)
    lines.append(f"- Total events: {total_events}")
    lines.append(f"- Total turns: {total_turns}")
    lines.append(f"- Duration: {_format_duration(duration_s)}")

    if result.get("profile_loaded") is True:
        fp = result.get("profile_fingerprint") or "(unknown)"
        psv = result.get("profile_schema_version")
        psv_str = str(psv) if isinstance(psv, int) else "(unknown)"
        lines.append(
            f"- Profile: `{fp}` (schema v{psv_str}, loaded)"
        )
    lines.append("")

    # Limited-signal notice prepended above section bodies.
    if status == PULSE_STATUS_LIMITED:
        usable, total = _count_usable_sub_analyzers(result)
        lines.append(
            f"> **Limited signal** — {usable} of {total} analyzers had "
            "usable data."
        )
        lines.append("")

    if status == PULSE_STATUS_NO_SIGNAL:
        # FIX-1 (v0.2.8): canonical live-session-first explanation.
        lines.append(
            "> **No usable analyzer signal in this session.** Per-turn "
            "token data is written when the Claude Code session ends. "
            "This session may still be running. End it, or run this "
            "against a previously ended session, then retry. If the "
            "session already ended, run `sentience init claude-code` "
            "and start a new session."
        )
        lines.append("")
        footer = _pulse_markdown_footer(result)
        if footer:
            lines.append(footer)
        return "\n".join(lines).rstrip() + "\n"

    # Section: undeclared-intent.
    undeclared = result.get("undeclared_intent") or {}
    lines.append("## Undeclared-intent spend")
    lines.append("")
    u_status = undeclared.get("status", "")
    total = int(undeclared.get("total_tokens", 0) or 0)
    u_tokens = int(undeclared.get("undeclared_tokens", 0) or 0)
    d_tokens = int(undeclared.get("declared_tokens", 0) or 0)
    pct = float(undeclared.get("undeclared_percent", 0.0) or 0.0)
    has_intent = bool(
        undeclared.get("session_has_declared_intent", False)
    )
    if u_status in (STATUS_NO_TOKEN_DATA, STATUS_NO_TURNS):
        if u_status == STATUS_NO_TOKEN_DATA:
            lines.append(
                "No per-turn token data was available for this session."
            )
        else:
            lines.append(
                "No reasoning turns with `CONTEXT_SNAPSHOT` identifiers "
                "were found."
            )
        lines.append("")
        lines.append(
            "_Why it matters: per-turn attribution requires turn-identified "
            "context snapshots — none were present._"
        )
    else:
        lines.append("| Metric | Tokens | Share |")
        lines.append("|---|---:|---:|")
        lines.append(f"| Total compute | {total:,} | 100.0% |")
        lines.append(f"| Undeclared | {u_tokens:,} | {pct:.1f}% |")
        lines.append(
            f"| Declared | {d_tokens:,} | {100.0 - pct:.1f}% |"
        )
        # IR-4 (v0.2.8.1): four token classes (sum to total compute).
        md_bd = undeclared.get("token_breakdown") or {}
        if total > 0 and md_bd:
            for _label, _key in (
                ("Cached read", "cached_read"),
                ("Cached write", "cached_write"),
                ("Prompt", "prompt"),
                ("Completion", "completion"),
            ):
                _v = int(md_bd.get(_key, 0) or 0)
                _share = (_v / total * 100.0) if total > 0 else 0.0
                lines.append(f"| {_label} | {_v:,} | {_share:.1f}% |")
        lines.append("")
        # IR-2 (v0.2.8.1): dedupe methodology note.
        if total > 0 and md_bd:
            lines.append("_Per-turn usage is deduped by requestId._")
            lines.append("")
        # Branch on the actual undeclared/total distribution. See the
        # CLI renderer for the four-case rationale.
        if total > 0 and u_tokens == total and not has_intent:
            lines.append(
                "_Why it matters: every attributed turn is classified as "
                "undeclared. Often a surface-bound limitation (e.g. "
                "Claude Code today), not agent drift._"
            )
        elif u_tokens > 0:
            lines.append(
                "_Why it matters: this is where agent work became "
                "harder to attribute._"
            )
        elif total > 0:
            lines.append(
                "_Why it matters: every turn was attributable to your "
                "declared session intent._"
            )
        else:
            lines.append(
                "_Why it matters: the trace had reasoning turns but no "
                "populated tokens — review the trace shape._"
            )
    # F21 (v0.2.9): tool-call counts — independent of token data, so it
    # renders on the no_token_data / no_turns branch too.
    md_tc = _pulse_tool_calls_md_lines(undeclared)
    if md_tc:
        lines.append("")
        lines.extend(md_tc)
    # IR-3 (v0.2.9): measured tool-token attribution (A1 + A2). Only renders
    # when token data is present.
    md_attr = _pulse_tool_token_attr_md_lines(undeclared)
    if md_attr:
        lines.append("")
        lines.extend(md_attr)
    lines.append("")

    # Section: burn rate.
    burn = result.get("policy_violations_burn_rate") or {}
    lines.append("## Policy-violation burn rate")
    lines.append("")
    b_status = burn.get("status", "")
    if b_status in (STATUS_NO_TOKEN_DATA, STATUS_NO_TURNS):
        if b_status == STATUS_NO_TOKEN_DATA:
            lines.append(
                "No per-turn token data — burn-rate attribution "
                "unavailable."
            )
        else:
            lines.append("No reasoning turns were detected.")
        lines.append("")
        lines.append(
            "_Why it matters: this is a surface-bound limitation, "
            "not a clean session._"
        )
    elif b_status == STATUS_NO_VIOLATIONS:
        lines.append("No policy violations recorded.")
        lines.append("")
        lines.append(
            "_Why it matters: your profile rules did not fire in "
            "this session._"
        )
    else:
        # ok / partial — full breakdown.
        lines.append(
            "Compute associated with turns where policy rules fired."
        )
        lines.append("")
        firing_turns = int(burn.get("violation_firing_turns", 0) or 0)
        assoc_tokens = int(burn.get("violation_associated_tokens", 0) or 0)
        lines.append(
            f"- {firing_turns} violation-firing "
            f"{'turn' if firing_turns == 1 else 'turns'} · "
            f"{assoc_tokens:,} tokens"
        )
        lines.append("")
        by_rule = burn.get("by_rule") or {}
        if by_rule:
            lines.append("| Rule | Turns | Tokens | Description |")
            lines.append("|---|---:|---:|---|")
            for rule in sorted(by_rule.keys()):
                slot = by_rule[rule] or {}
                lines.append(
                    f"| `{rule}` | {int(slot.get('turn_count', 0) or 0)} | "
                    f"{int(slot.get('token_cost', 0) or 0):,} | "
                    f"{slot.get('description') or ''} |"
                )
            lines.append("")
        # Markdown gets the full `notes` (verbose) per plan v3.3.
        for note in burn.get("notes") or []:
            lines.append(f"> {note}")
            lines.append("")
        if by_rule:
            primary_rule, primary_slot = max(
                sorted(by_rule.items()),
                key=lambda kv: int(
                    (kv[1] or {}).get("token_cost", 0) or 0
                ),
            )
            primary_tc = int(
                (primary_slot or {}).get("token_cost", 0) or 0
            )
            lines.append(
                f"_Why it matters: `{primary_rule}` appeared on turns "
                f"representing {primary_tc:,} tokens. This is the first "
                "rule to inspect if you want tighter agent boundaries._"
            )
    lines.append("")

    # Section: advisory flags.
    summary_flags = result.get("advisory_flag_summary") or {}
    lines.append("## Advisory flags")
    lines.append("")
    nonzero = sorted(
        [(k, int(v or 0)) for k, v in summary_flags.items() if (v or 0) > 0]
    )
    if not nonzero:
        lines.append("None fired in this session.")
        lines.append("")
        lines.append(
            "_Why it matters: no profile-driven advisory thresholds "
            "were crossed._"
        )
    else:
        lines.append("| Flag | Count |")
        lines.append("|---|---:|")
        for flag, count in nonzero:
            lines.append(f"| `{flag}` | {count} |")
        lines.append("")
        boundary = summary_flags.get("TASK_BOUNDARY_CROSSED", 0) or 0
        high = summary_flags.get("HIGH_CONSEQUENCE_DETECTED", 0) or 0
        parts = []
        if boundary:
            parts.append(
                f"the agent crossed into new tasks {boundary} "
                f"time{'s' if boundary != 1 else ''}"
            )
        if high:
            parts.append(
                f"triggered {high} high-consequence "
                f"operation{'s' if high != 1 else ''}"
            )
        if parts:
            lines.append(
                "_Why it matters: " + " and ".join(parts)
                + ". Review the trace for context._"
            )
        else:
            lines.append(
                "_Why it matters: review the trace for any of the above "
                "advisory signals._"
            )
    lines.append("")

    # Interpretation block — mandatory for the clean-session path.
    if b_status == STATUS_NO_VIOLATIONS:
        lines.append("## Interpretation")
        lines.append("")
        lines.append(_CLEAN_SESSION_INTERPRETATION)
        lines.append("")

    # Email-list footer (Markdown form).
    footer = _pulse_markdown_footer(result)
    if footer:
        lines.append(footer)

    return "\n".join(lines).rstrip() + "\n"


def _pulse_markdown_footer(result: Dict[str, Any]) -> Optional[str]:
    """Render the email-list footer as a single Markdown
    block (italic) iff ``result.sync_prompt.show`` is truthy.
    """
    sync_prompt = result.get("sync_prompt") or {}
    if not sync_prompt.get("show"):
        return None
    return (
        "---\n"
        "\n"
        "_Want this pulse delivered weekly via email?_  \n"
        "_Join the list: "
        "[getsentience.ai/sentience-sync](https://getsentience.ai/sentience-sync)_"
    )


# ---------------------------------------------------------------------------
# Retrospective session reader (v0.3.1) — `sentience scan` / `/sentience-review`
#
# The screens are the product hypothesis, not incidental renderer output
# (plan §8.1). The strings below marked normative are locked plan text and
# must not be reworded here; the report is a *reviewer's* account, never a
# governance verdict.
# ---------------------------------------------------------------------------

_SCAN_HEADER = "Sentience · Retrospective Review"

# Normative (§8.1, test 51b): the single canonical reviewer statement, on
# every screen including State N. No state may substitute a shorthand.
_REVIEWER_STATEMENT = (
    "Reader is a retrospective reviewer, not live governance.\n"
    "It cannot establish what you intended or authorized, or\n"
    "whether an action complied with policy."
)

# Normative (§7.2, test 50): attempt-grade language. An assistant record
# proves Claude *issued* the write; completion evidence lives in
# `toolUseResult`, which Reader never inspects.
_WRITE_QUALIFIER = (
    "Counts are write operations Claude issued as recorded in\n"
    "its history. Reader does not verify completion."
)

# Normative (§8.1/§8.3): the identical closing bridge on all screens.
_CLOSING_BRIDGE = (
    "This review is an example of what retrospective history\n"
    "can reveal. Sentience Governor goes further by evaluating\n"
    "declared objective, scope, execution and policy while the\n"
    "agent is running."
)

_SCAN_FOOTER = "  sentience init claude-code        govern the next session"

_PROMPT_EXCLUSION = "Reader does not inspect or use your prompt content."

# Normative (§8.1, test 51): State 0 leads with what was *found*, never with
# confinement ("everything stayed inside"). A successful zero screen, not
# reassurance — never "0 findings", never "nothing to report".
_STATE_0_LEAD = (
    "No reportable project-boundary write activity was found\n"
    "in the activity Reader could classify."
)

# A presentation line, not a reviewer-statement substitute (§8.1).
_LIST_CAPTION = "Showing the strongest retrospective findings first."

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Two pinned forms, no other variation (§4.5), shared by the summary and
# the detail view. Reader knows the destination roots, so plurality here
# is grammar, not an aggregate project count.
_CROSS_ONE = "Claude targeted writes into another project directory:"
_CROSS_MANY = "Claude targeted writes into other project directories:"

_DEST_ROWS_CAP = 10          # per-session destination rows (§8.4)
_DETAIL_SESSIONS = 3         # State N: detail for the strongest few
_BODY_WIDTH = 52


def _scan_date(iso: str) -> str:
    from datetime import datetime as _dt
    parsed = _dt.fromisoformat(iso)
    return "%s %d" % (_MONTHS[parsed.month - 1], parsed.day)


def _abbrev(path: str, home: str) -> str:
    """Display form of a path: home-anchored paths render as ``~/…``."""
    if not path:
        return path
    if home:
        home = home.rstrip("/")
        if path == home:
            return "~"
        if path.startswith(home + "/"):
            return "~" + path[len(home):]
    return path


def _ops(count: int) -> str:
    return "1 op" if count == 1 else "%d ops" % count


def _wrap_body(text: str, indent: str = "") -> List[str]:
    import textwrap
    return textwrap.wrap(
        text, width=_BODY_WIDTH + len(indent),
        initial_indent=indent, subsequent_indent=indent,
    ) or [indent.rstrip()]


def _dest_rows(pairs: List[tuple], home: str) -> List[str]:
    """Destination rows, strongest first, truncated with an explicit line."""
    ordered = sorted(pairs, key=lambda item: (-item[1], item[0]))
    rows = []
    for path, count in ordered[:_DEST_ROWS_CAP]:
        label = _abbrev(path, home)
        rows.append("    " + label.ljust(28) + "%4d write operation%s" % (
            count, "" if count == 1 else "s"))
    remaining = len(ordered) - _DEST_ROWS_CAP
    if remaining > 0:
        rows.append("    … and %d more destinations" % remaining)
    return rows


def _session_findings(findings: List, session_id: str) -> List:
    return [f for f in findings if f.session_id == session_id]


def _group_totals(findings: List, key) -> List[tuple]:
    totals: Dict[str, int] = {}
    for finding in findings:
        bucket = key(finding)
        totals[bucket] = totals.get(bucket, 0) + finding.op_count
    return list(totals.items())


def _session_label_line(session_id: str, labels: Dict, indent: str) -> str:
    """The session's label. The fallback must look deliberate (§7.3), so an
    8-character session ID renders bare rather than quoted."""
    entry = labels.get(session_id) or {}
    label = entry.get("label") or session_id[:8]
    if entry.get("source") == "session-id" or not entry:
        return indent + label
    return indent + '"%s"' % label


def _cross_project_pairs(session_findings: List) -> List[tuple]:
    return _group_totals(
        [f for f in session_findings if f.finding_class == "cross_project"],
        lambda f: f.dest_root,
    )


def _non_project_pairs(session_findings: List) -> List[tuple]:
    # Grouped by the target's containing directory: Reader cannot identify a
    # project for these, so the directory is the honest unit of "location".
    import posixpath
    return _group_totals(
        [f for f in session_findings if f.finding_class == "non_project"],
        lambda f: posixpath.dirname(f.target),
    )


def _location_prefix(target: str, home: str) -> str:
    """The display prefix for an unidentifiable location.

    A plain truncation to the first path component, never an inferred
    root: several descendants of one old tree collapse to one honest
    example line instead of implying Reader knows how many distinct
    "locations" there were.
    """
    shown = _abbrev(target, home)
    parts = [c for c in shown.split("/") if c]
    if shown.startswith("~") and len(parts) >= 2:
        return "~/%s/…" % parts[1]
    if shown.startswith("/") and parts:
        return "/%s/…" % parts[0]
    return shown


def _representative_locations(findings: List, home: str,
                              limit: int = 5) -> List[str]:
    totals: Dict[str, int] = {}
    for finding in findings:
        prefix = _location_prefix(finding.target, home)
        totals[prefix] = totals.get(prefix, 0) + finding.op_count
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    return [prefix for prefix, _ in ordered[:limit]]


def _non_project_ops(findings: List) -> int:
    return sum(finding.op_count for finding in findings)


def _session_block(session_id: str, findings: List, labels: Dict,
                   home: str) -> List[str]:
    """One session in the State-1 shape: label, working-in, then evidence."""
    mine = _session_findings(findings, session_id)
    lines = [_session_label_line(session_id, labels, "  ")]
    source_root = next((f.source_root for f in mine if f.source_root), "")
    if source_root:
        lines.append("   working in  " + _abbrev(source_root, home))

    cross = _cross_project_pairs(mine)
    non_project = _non_project_pairs(mine)

    if cross:
        lines.append("")
        # The two pinned forms (§4.5), shared with the detail view so one
        # screen never says "project directory" and the other "projects".
        # Grammatical agreement only: no aggregate destination count, which
        # `dest_root` makes calculable but which Reader does not claim.
        lines.append("  " + (_CROSS_ONE if len(cross) == 1 else _CROSS_MANY))
        lines.append("")
        lines.extend(_dest_rows(cross, home))

    unidentified = [f for f in mine if f.finding_class == "non_project"]
    if unidentified:
        lines.append("")
        # Never "outside any project": the claim is only about what Reader
        # can identify now (§5.2, §8.1). Reported as operation VOLUME plus
        # representative destinations — never as a count of distinct
        # locations, which Reader does not know: descendants of one old
        # tree would inflate it.
        ops = _non_project_ops(unidentified)
        if cross:
            headline = ("and %d write operations targeted locations where "
                        "Reader cannot identify a project today:" % ops)
        else:
            headline = ("Claude targeted %d write operations into locations "
                        "where Reader cannot identify a project today:" % ops)
        lines.extend(_wrap_body(headline, indent="  "))
        lines.append("")
        for prefix in _representative_locations(unidentified, home):
            lines.append("    " + prefix)
        lines.append("")
        lines.append("  Showing representative destinations.")

    return lines


def _claude_config_sentence(config_findings: List, home: str) -> str:
    sessions = {f.session_id for f in config_findings}
    targets: List[str] = []
    for finding in config_findings:
        shown = _abbrev(finding.target, home)
        if shown not in targets:
            targets.append(shown)
    where = ("In one session" if len(sessions) == 1
             else "In %d sessions" % len(sessions))
    verb = ("targeted a write" if len(config_findings) == 1
            else "targeted writes")
    if len(targets) == 1:
        listed = targets[0]
    else:
        listed = ", ".join(targets[:2])
        if len(targets) > 2:
            listed += ", and %d more" % (len(targets) - 2)
    return ("%s, Claude %s to its global configuration (%s), which can "
            "affect future Claude sessions." % (where, verb, listed))


def _coverage_limits(result: Dict[str, Any]) -> List[str]:
    """Coverage limits, shown only when non-zero (§8.2)."""
    parts = []
    scanned = result.get("files_scanned", 0)
    unreadable = result.get("files_unreadable", 0)
    if unreadable:
        parts.append("%d of %d files read" % (scanned, scanned + unreadable))
        parts.append("%d unreadable" % unreadable)
    for count, text in (
        (result.get("lines_malformed", 0), "malformed records skipped"),
        (result.get("lines_oversize", 0), "oversize records skipped"),
        (result.get("records_undated", 0), "undated records"),
        (result.get("unknown_targets", 0), "targets Reader could not classify"),
        (result.get("unknown_root_writes", 0),
         "writes whose source project could not be identified"),
    ):
        if count:
            parts.append("%d %s" % (count, text))
    suppressed = sum((result.get("suppressed") or {}).values())
    if suppressed:
        parts.append("%d auto-maintained records suppressed" % suppressed)
    if result.get("findings_omitted", 0):
        parts.append("%d findings not shown" % result["findings_omitted"])
    if result.get("targets_omitted", 0):
        parts.append(
            "%d targets beyond the collection bound were not ranked"
            % result["targets_omitted"])
    if not parts:
        return []
    return _wrap_body(" · ".join(parts), indent="  ")


def _not_evaluated(result: Dict[str, Any], standout: List[str],
                   review_wide: bool = False) -> List[str]:
    """Bash and coverage limits, always outside the aha paragraph (§8.1).

    ``review_wide`` scopes the shell count to the whole review rather than
    to the standout sessions: the detail view covers every session
    carrying findings plus an accounting of the rest, so a
    per-standout-session count would understate what was not evaluated.
    """
    by_session = result.get("by_session") or {}
    if review_wide:
        shell_line = ("%s shell commands across the review"
                      % format(result.get("shell_calls", 0), ","))
    elif len(standout) == 1:
        shell = by_session.get(standout[0], {}).get("shell_calls", 0)
        shell_line = "%s shell commands in this session" % format(shell, ",")
    elif standout:
        shell = sum(by_session.get(sid, {}).get("shell_calls", 0)
                    for sid in standout)
        shell_line = "%s shell commands across these sessions" % format(shell, ",")
    else:
        shell_line = "%s shell commands" % format(result.get("shell_calls", 0), ",")

    lines = ["Not evaluated", "  " + shell_line, "  " + _PROMPT_EXCLUSION]
    lines.extend(_coverage_limits(result))
    return lines


_DETAIL_HEADER = "Sentience · Retrospective Review — evidence"

_STANDOUT_SECTION = "Sessions that stand out"
_OTHER_SECTION = "Other reviewed sessions"

# Neutral: states the selection fact and nothing more. Must never imply the
# sessions were safe, normal, unimportant, or that they stood out (§4.4).
_OTHER_SECTION_NOTE = (
    "These sessions did not meet Reader's standout criteria. They\n"
    "are included because this view shows the reviewed findings."
)

_CONFIG_HEADING = "Claude targeted writes to its global configuration:"

# The continuation line (§4.1). Offered only when detail would show
# something, so a clean review never advertises an empty page.
_DETAIL_CONTINUATION = "  Inspect the evidence: sentience scan --detail"


def _relative_to(target: str, root: str) -> str:
    """A cross-project target shown relative to its destination root.

    The root is stated once in the orientation line; repeating it on every
    row is the only real noise in a long block (§4.8). Falls back to the
    absolute form when the target is not under the root.
    """
    if root and target.startswith(root.rstrip("/") + "/"):
        return target[len(root.rstrip("/")) + 1:]
    return target


def _finding_rows(findings: List, home: str, root: str = "") -> List[str]:
    """Operation count then path, one row per finding, incoming rank order."""
    rows = []
    for finding in findings:
        shown = (_relative_to(finding.target, root) if root
                 else _abbrev(finding.target, home))
        rows.append("     %5d  %s" % (finding.op_count, shown))
    return rows


def _detail_session_block(session_id: str, findings: List, labels: Dict,
                          home: str) -> List[str]:
    """One session's evidence: orient each group, then list its findings.

    Paths are never dumped directly under a session (§4.5): every group
    opens with an aggregate line built from what Reader already holds.
    """
    mine = _session_findings(findings, session_id)
    lines = [_session_label_line(session_id, labels, "  ")]
    source_root = next((f.source_root for f in mine if f.source_root), "")
    if source_root:
        lines.append("   working in  " + _abbrev(source_root, home))

    config = [f for f in mine if f.finding_class == "claude_config"]
    cross = [f for f in mine if f.finding_class == "cross_project"]
    unidentified = [f for f in mine if f.finding_class == "non_project"]

    if config:
        lines.append("")
        lines.append("  " + _CONFIG_HEADING)
        lines.append("")
        lines.extend(_finding_rows(config, home))

    if cross:
        # Subgrouped by the destination root Reader already recorded: one
        # flat list would lose which project each path belongs to (§4.5).
        roots: Dict[str, List] = {}
        for finding in cross:
            roots.setdefault(finding.dest_root, []).append(finding)
        lines.append("")
        lines.append("  " + (_CROSS_ONE if len(roots) == 1 else _CROSS_MANY))
        ordered = sorted(
            roots.items(),
            key=lambda item: (-sum(f.op_count for f in item[1]), item[0]),
        )
        for root, items in ordered:
            lines.append("")
            lines.append("    %s — %d write operation%s" % (
                _abbrev(root, home), sum(f.op_count for f in items),
                "" if sum(f.op_count for f in items) == 1 else "s"))
            lines.append("")
            lines.extend(_finding_rows(items, home, root=root))

    if unidentified:
        # Volume only. Never a count of distinct targets, destinations,
        # locations, trees or projects — Reader does not know it (§2.5).
        lines.append("")
        lines.extend(_wrap_body(
            "%d write operations targeting locations where Reader cannot "
            "identify a project today:" % _non_project_ops(unidentified),
            indent="  "))
        lines.append("")
        lines.extend(_finding_rows(unidentified, home))

    return lines


def _reviewed_accounting(result: Dict[str, Any], with_findings: int) -> List[str]:
    """One line for the reviewed sessions that produce no detail section.

    Never N empty sections, and never a claim that those sessions were
    clean: Reader reports only what it can classify (§4.4).
    """
    total = result.get("sessions", 0)
    remainder = total - with_findings
    if remainder <= 0:
        return []
    return _wrap_body(
        "The other %d reviewed session%s produced no findings. That is not "
        "a statement that they were clean: Reader reports only what it can "
        "classify, and it did not evaluate the shell commands in any "
        "session." % (remainder, "" if remainder == 1 else "s"),
        indent="")


def render_scan_detail(result: Dict[str, Any]) -> str:
    """Render the evidence behind a scan review (plan §4.4).

    The same review at greater depth, never a different document with
    weaker framing: the reviewer statement, the ``Not evaluated`` block and
    the closing bridge are carried unchanged. Pure, and reads the same
    ``result`` as :func:`render_scan` including the private ``_home``.
    """
    findings = list(result.get("findings") or [])
    labels = result.get("session_labels") or {}
    home = result.get("_home") or ""
    standout = list(result.get("sessions_with_findings") or [])

    # Ordering comes from the shipped session ranking, carried as private
    # renderer metadata beside ``_home``; the findings themselves arrive in
    # ``rank_findings`` order and are never reordered here.
    ranked = list(result.get("_ranked_sessions") or standout)
    carrying = [sid for sid in ranked
                if any(f.session_id == sid for f in findings)]
    other = [sid for sid in carrying if sid not in standout]

    sessions = result.get("sessions", 0)
    lines = [_DETAIL_HEADER, ""]
    lines.append("%d Claude Code session%s reviewed"
                 % (sessions, "" if sessions == 1 else "s"))
    period_start, period_end = result.get("period_start"), result.get("period_end")
    provenance = "local · transcripts read-only"
    if period_start and period_end:
        lines.append("%s → %s · %s" % (_scan_date(period_start),
                                       _scan_date(period_end), provenance))
    else:
        lines.append(provenance)

    if not findings:
        lines.append("")
        lines.extend(_STATE_0_LEAD.split("\n"))
        lines.append("")
        lines.extend(_REVIEWER_STATEMENT.split("\n"))
        lines.append("")
        lines.extend(_not_evaluated(result, standout, review_wide=True))
        lines.append("")
        lines.extend(_CLOSING_BRIDGE.split("\n"))
        return "\n".join(lines) + "\n"

    if standout:
        lines.append("")
        lines.append(_STANDOUT_SECTION)
        for session_id in standout:
            lines.append("")
            lines.extend(_detail_session_block(session_id, findings, labels, home))

    if other:
        lines.append("")
        lines.append(_OTHER_SECTION)
        lines.append("")
        lines.extend(
            "  " + part for part in _OTHER_SECTION_NOTE.split("\n"))
        for session_id in other:
            lines.append("")
            lines.extend(_detail_session_block(session_id, findings, labels, home))

    accounting = _reviewed_accounting(result, len(carrying))
    if accounting:
        lines.append("")
        lines.extend(accounting)

    lines.append("")
    lines.extend(_WRITE_QUALIFIER.split("\n"))
    lines.append("")
    lines.extend(_REVIEWER_STATEMENT.split("\n"))
    lines.append("")
    lines.extend(_not_evaluated(result, standout, review_wide=True))
    lines.append("")
    lines.extend(_CLOSING_BRIDGE.split("\n"))
    return "\n".join(lines) + "\n"


def render_scan(result: Dict[str, Any]) -> str:
    """Render a :func:`sentience_governor.retro.scan` result (plan §8).

    Three first-class states, selected by how many sessions stand out —
    never by finding count, which is never explained to a first-run user.
    Pure: every input, including the private ``_home`` prefix used to
    abbreviate paths for display, arrives in ``result``.
    """
    findings = list(result.get("findings") or [])
    labels = result.get("session_labels") or {}
    home = result.get("_home") or ""
    standout = list(result.get("sessions_with_findings") or [])

    sessions = result.get("sessions", 0)
    lines = [_SCAN_HEADER, ""]
    lines.append("%d Claude Code session%s reviewed"
                 % (sessions, "" if sessions == 1 else "s"))
    period_start, period_end = result.get("period_start"), result.get("period_end")
    provenance = "local · transcripts read-only"
    if period_start and period_end:
        lines.append("%s → %s · %s" % (_scan_date(period_start),
                                       _scan_date(period_end), provenance))
    else:
        lines.append(provenance)
    lines.append("")

    # Rank-1 findings get their own sentence, first, in any state (§8.2).
    config_findings = [f for f in findings if f.finding_class == "claude_config"]
    if config_findings:
        lines.extend(_wrap_body(_claude_config_sentence(config_findings, home)))
        lines.append("")

    if not standout:
        lines.extend(_STATE_0_LEAD.split("\n"))
        lines.append("")
        lines.extend(_REVIEWER_STATEMENT.split("\n"))
    elif len(standout) == 1:
        # The hero screen: the surprising human-readable fact first, then
        # the evidence, then the limitations. No padding.
        lines.append("One session stands out.")
        lines.append("")
        lines.extend(_session_block(standout[0], findings, labels, home))
        lines.append("")
        lines.extend(_WRITE_QUALIFIER.split("\n"))
        lines.append("")
        lines.extend(_REVIEWER_STATEMENT.split("\n"))
    else:
        lines.append("%d sessions stand out." % len(standout))
        lines.append("")
        for position, session_id in enumerate(standout, start=1):
            mine = _session_findings(findings, session_id)
            label = _session_label_line(session_id, labels, "").strip()
            lines.append("  %d. %s" % (position, label))
            source_root = next((f.source_root for f in mine if f.source_root), "")
            if source_root:
                lines.append("     working in  " + _abbrev(source_root, home))
            cross = _cross_project_pairs(mine)
            non_project = _non_project_pairs(mine)
            if cross:
                lines.append("     targeted writes into %d other project%s"
                             % (len(cross), "" if len(cross) == 1 else "s"))
            elif non_project:
                lines.extend(_wrap_body(
                    "targeted %d write operations into locations where "
                    "Reader cannot identify a project today"
                    % _non_project_ops(
                        [f for f in mine if f.finding_class == "non_project"]),
                    indent="     "))
            else:
                lines.append("     targeted a write to its global configuration")
            lines.append("")
        lines.append(_LIST_CAPTION)
        lines.append("")
        # Rendered directly under the caption, where verdict-reading would
        # otherwise happen (§8.1).
        lines.extend(_REVIEWER_STATEMENT.split("\n"))
        for session_id in standout[:_DETAIL_SESSIONS]:
            lines.append("")
            lines.extend(_session_block(session_id, findings, labels, home))
        remaining = len(standout) - _DETAIL_SESSIONS
        if remaining > 0:
            lines.append("")
            lines.append("  … and %d more session%s not detailed here"
                         % (remaining, "" if remaining == 1 else "s"))
        lines.append("")
        lines.extend(_WRITE_QUALIFIER.split("\n"))

    declaration = result.get("sessions_with_declaration_activity") or []
    if declaration:
        lines.append("")
        lines.append("Declared-intent activity in %d of %d sessions."
                     % (len(declaration), sessions))

    lines.append("")
    lines.extend(_not_evaluated(result, standout))
    # The evidence path, named on the screen so it is never something the
    # operator has to discover from CLI help (§4.1). Offered only when
    # detail would show something.
    if findings:
        lines.append("")
        lines.append(_DETAIL_CONTINUATION)
    lines.append("")
    lines.extend(_CLOSING_BRIDGE.split("\n"))
    lines.append("")
    lines.append(_SCAN_FOOTER)
    return "\n".join(lines) + "\n"
