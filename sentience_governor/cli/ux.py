"""`sentience` — signal-first trace viewer for agent-hook sessions.

Purpose
-------
Opinionated CLI for the high-noise, high-volume agent-hook workflow
(Claude Code today; Cursor / Codex / OpenCode in the future). Three
subcommands:

* ``sentience status`` — self-check. Is the hook capturing sessions?
* ``sentience list`` — what sessions exist? One line per session.
* ``sentience open [--latest | <session_id>]`` — curated view of one
  session with Summary, Focus block, Notes, Key Events, Full Trace,
  Footer.

Relationship to ``sentience-cli``
---------------------------------
Two CLIs, two audiences.

* ``sentience-cli <file>`` (``sentience_governor.cli.viewer``) is the
  raw, full-fidelity viewer. Every event renders with detail. Used by
  library integrators (MCP wrapper, LangChain) and by the
  golden-trace snapshot tests. Stays unchanged.

* ``sentience`` (this module) is the curated, narrative viewer. Uses
  signal-hierarchy rendering with a baseline-noise classifier so
  hundreds of near-identical events in a Claude Code session compress
  into a scannable story.

Implementation notes
--------------------
* Single-file v1. Classifier, formatter, render blocks, and CLI
  dispatch all live here. Can split into submodules later if any one
  piece grows.
* Reuses ``parse_events`` from :mod:`sentience_governor.cli.viewer`
  for the trace parsing primitive — no duplicate JSONL handling.
* Reuses the default sink path from
  :mod:`sentience_governor.wrapper.claude_code_hook` so a user who
  has not set ``SENTIENCE_CLAUDE_CODE_SINK_PATH`` gets the same path
  the hook writes to.
* Fail-safe: unknown tools, missing fields, corrupt JSON lines all
  render as ``???`` rather than crashing. Forward-compatible with
  future adapters.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from sentience_governor.analyze import (
    compute_policy_violation_burn_rate,
    compute_pulse,
    compute_undeclared_intent_spend,
    render_burn_rate_cli,
    render_burn_rate_markdown,
    render_cli as render_undeclared_cli,
    render_markdown_report as render_undeclared_markdown,
    render_pulse_cli,
    render_pulse_markdown,
)
from sentience_governor.cli.first_run import maybe_run_first_run_flow
from sentience_governor.cli.viewer import parse_events
from sentience_governor.profile import GovernanceProfile
from sentience_governor.profile.loader import DEFAULT_PROFILE_PATH

# ---------------------------------------------------------------------------
# Defaults — mirror what the Claude Code hook writes to.
# Imported lazily so test suites that patch env vars still see them.
# ---------------------------------------------------------------------------

_DEFAULT_SINK_DIR = Path.home() / ".sentience" / "traces" / "claude-code"


def _resolve_trace_dir() -> Path:
    """Return the trace directory the hook is writing to.

    Honours the same env var the Claude Code hook honours
    (``SENTIENCE_CLAUDE_CODE_SINK_PATH``) with the same disambiguation
    rule: ``.jsonl`` suffix = shared-file mode (return parent
    directory); anything else = per-session directory mode.
    """
    raw = os.environ.get("SENTIENCE_CLAUDE_CODE_SINK_PATH", "").strip()
    if not raw:
        return _DEFAULT_SINK_DIR
    base = Path(raw).expanduser()
    if base.suffix == ".jsonl":
        return base.parent
    return base


# ---------------------------------------------------------------------------
# Gloss table — LOCKED. Every advisory / policy code here is part of
# the CLI's stable output contract and must not drift.
# ---------------------------------------------------------------------------

_GLOSS: Dict[str, str] = {
    "POL-001": "write operation without declared intent",
    "POL-002": "agent not registered",
    "POL-003": "unclassified context",
    "POL-004": "memory write without classification or retention policy",
    "POL-005": "sensitivity escalation without authorization",
    "INTENT_MISSING": "no intent declared",
    "SCOPE_INTENT_MISMATCH": "tool call does not match declared intent",
    "SCOPE_OPERATION_UNEXPECTED": "unexpected operation type for declared scope",
    "CONTEXT_UNCLASSIFIED": "context data not classified",
    "SENSITIVITY_ESCALATION": "context sensitivity escalated",
    "MEMORY_WRITE_CANDIDATE": "write outside declared scope",
    "MEMORY_WRITE_UNCLASSIFIED": "memory write lacks classification",
    "AGENT_UNREGISTERED": "agent not registered before session start",
}


def _gloss(code: str) -> str:
    """Return the plain-English gloss for a code; empty string if unknown.

    Per §5.5: if a new code appears that is not in the table, render
    the code alone (no invented wording).
    """
    return _GLOSS.get(code, "")


# ---------------------------------------------------------------------------
# Baseline-noise classifier — §6.1 of the plan.
# ---------------------------------------------------------------------------

# Known baseline patterns: (event_type, code) where code may be an
# advisory flag OR a policy violation. Both forms appear on the wire.
_BASELINE_PATTERNS: set = {
    ("INTENT_DECLARED", "INTENT_MISSING"),
    ("CONTEXT_SNAPSHOT", "POL-003"),
    ("CONTEXT_SNAPSHOT", "CONTEXT_UNCLASSIFIED"),
}

_BASELINE_FREQUENCY_THRESHOLD = 0.80


@dataclass
class _ClassifiedSession:
    """Result of classifying one session's events.

    ``anomalies`` holds one entry per event that carries at least one
    anomaly code. ``baseline_codes_present`` records which known
    baseline patterns crossed the 80% threshold in this session.

    Focus is derived from ``anomalies`` (§5.2 coupling rule). Summary
    is derived from ``anomaly_code_counts``. Both come from the same
    source; they cannot drift.
    """

    events: List[dict]
    anomalies: List["_EventAnomaly"] = field(default_factory=list)
    anomaly_code_counts: Counter = field(default_factory=Counter)
    baseline_codes_present: set = field(default_factory=set)
    tool_counts: Counter = field(default_factory=Counter)


@dataclass
class _EventAnomaly:
    """One event's worth of anomaly data, carried through to render."""

    sequence: int
    event_type: str
    tool_name: str
    tool_input: dict
    code: str  # the primary (highest-priority) anomaly code
    all_codes: List[str]
    user_impact_rank: int  # lower is more severe per §5.3 ordering
    raw_event: dict = field(default_factory=dict)
    # raw_event is carried so Key Events / Focus refs can render
    # non-SCOPE events (MEMORY_WRITE_ATTEMPT, INTENT_DECLARED, etc.)
    # via _format_event_oneline rather than the tool-action path.


def _user_impact_rank(event_type: str, code: str, tool_name: str) -> int:
    """Rank an anomaly by user impact (§5.3 ordering).

    Lower is more severe. Ordering:
      1. Destructive operations (writes, deletes, memory writes)
      2. Unexpected command execution (Bash-class)
      3. Scope mismatches
      4. Context exposure issues
    Policy-code severity is a tiebreaker only.
    """
    if code in ("MEMORY_WRITE_CANDIDATE", "MEMORY_WRITE_UNCLASSIFIED", "POL-004"):
        return 1
    if event_type == "MEMORY_WRITE_ATTEMPT":
        return 1
    if tool_name == "Bash" and code in (
        "POL-001",
        "SCOPE_OPERATION_UNEXPECTED",
    ):
        return 2
    if code in ("SCOPE_INTENT_MISMATCH", "SCOPE_OPERATION_UNEXPECTED", "POL-001"):
        return 3
    if code in ("SENSITIVITY_ESCALATION", "POL-005"):
        return 4
    # Anything else (unknown codes, context flags not filtered as baseline)
    return 5


def classify_session(events: List[dict]) -> _ClassifiedSession:
    """Classify events into anomalies vs baseline noise.

    Pattern + frequency rule per §6.1:

    * A ``(event_type, code)`` tuple is baseline iff it matches a
      known-baseline pattern AND appears in >80% of eligible events
      of that event_type.
    * Everything else is anomaly.

    ``events`` must already be ordered by event_sequence_number. The
    caller (``parse_events``) guarantees this.
    """
    result = _ClassifiedSession(events=events)

    # Step 1 — compute per-event-type frequencies for each known pattern.
    events_by_type: Dict[str, List[dict]] = defaultdict(list)
    for ev in events:
        events_by_type[ev.get("event_type", "")].append(ev)

    baseline_eligible: set = set()  # (event_type, code) tuples that meet the rule
    for pattern in _BASELINE_PATTERNS:
        event_type, code = pattern
        eligible = events_by_type.get(event_type, [])
        if not eligible:
            continue
        matching = 0
        for ev in eligible:
            if _event_has_code(ev, code):
                matching += 1
        frequency = matching / len(eligible)
        if frequency > _BASELINE_FREQUENCY_THRESHOLD:
            baseline_eligible.add(pattern)
            result.baseline_codes_present.add(code)

    # Step 2 — walk events, classify each code as baseline or anomaly.
    for ev in events:
        event_type = ev.get("event_type", "")
        tool_name = _extract_tool_name(ev)
        if tool_name:
            result.tool_counts[tool_name] += 1

        event_codes = _all_event_codes(ev)
        anomaly_codes: List[str] = []
        for code in event_codes:
            if (event_type, code) in baseline_eligible:
                continue  # baseline noise
            anomaly_codes.append(code)

        if not anomaly_codes:
            continue

        # Pick the highest-priority anomaly code (lowest rank value).
        ranked = sorted(
            anomaly_codes,
            key=lambda c: _user_impact_rank(event_type, c, tool_name),
        )
        primary = ranked[0]
        for c in anomaly_codes:
            result.anomaly_code_counts[c] += 1

        result.anomalies.append(
            _EventAnomaly(
                sequence=ev.get("event_sequence_number", 0),
                event_type=event_type,
                tool_name=tool_name,
                tool_input=_extract_tool_input(ev),
                code=primary,
                all_codes=anomaly_codes,
                user_impact_rank=_user_impact_rank(event_type, primary, tool_name),
                raw_event=ev,
            )
        )

    return result


def _all_event_codes(event: dict) -> List[str]:
    """Flatten advisory_flags + policy_violations into a single list."""
    flags = event.get("advisory_flags") or []
    vios = event.get("policy_violations") or []
    out: List[str] = []
    if isinstance(flags, list):
        out.extend(str(x) for x in flags if x)
    if isinstance(vios, list):
        out.extend(str(x) for x in vios if x)
    return out


def _event_has_code(event: dict, code: str) -> bool:
    return code in _all_event_codes(event)


# FIX-3 (v0.2.8): the status/list surfaces must never print an advisory
# count under a "Violations" label (user guide §4.3 distinction; findings
# F2/F3 — the 78-vs-58 confusion). Partition the classifier's anomaly
# counts by code shape; expose the reconciliation via `status --json`.
_POL_CODE_RE = re.compile(r"^POL-\d{3}$")


def _split_anomaly_counts(cls: "_ClassifiedSession") -> Tuple[int, int]:
    """(policy_violation_total, advisory_flag_total) beyond baseline."""
    pol = sum(
        n for code, n in cls.anomaly_code_counts.items()
        if _POL_CODE_RE.match(code)
    )
    adv = sum(
        n for code, n in cls.anomaly_code_counts.items()
        if not _POL_CODE_RE.match(code)
    )
    return pol, adv


def _status_reconciliation(events: List[dict], cls: "_ClassifiedSession") -> dict:
    """The `status --json` audit fields (FIX-3 hard requirement):
    enough to reconcile raw vs displayed counts without re-deriving
    from trace files. raw_total == displayed (pol+adv) + baseline."""
    raw_counts: Counter = Counter()
    for ev in events:
        for code in _all_event_codes(ev):
            raw_counts[code] += 1
    baseline_filtered = {
        code: raw_counts[code] - cls.anomaly_code_counts.get(code, 0)
        for code in sorted(raw_counts)
        if raw_counts[code] - cls.anomaly_code_counts.get(code, 0) > 0
    }
    pol, adv = _split_anomaly_counts(cls)
    return {
        "policy_violations": pol,
        "advisory_flags": adv,
        "baseline_filtered": baseline_filtered,
        "baseline_filtered_total": sum(baseline_filtered.values()),
        "raw_total": sum(raw_counts.values()),
    }


# ---------------------------------------------------------------------------
# Locked event formatting table — §5.6 of the plan.
# Unknown tools fall back to `<tool_name> → ???`, NEVER crash.
# Tool identity is always preserved even when payload is unknown.
# ---------------------------------------------------------------------------


def _extract_tool_name(event: dict) -> str:
    """Return the tool_id from a SCOPE_ASSERTED payload, or empty string."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("tool_id") or "")


def _extract_tool_input(event: dict) -> dict:
    """Best-effort extraction of tool_input shape from either a native
    wrapper trace or a Claude Code hook trace.

    The library wrappers emit SCOPE_ASSERTED events without tool_input
    in the payload. The Claude Code hook also does not encode tool_input
    on SCOPE_ASSERTED (it appears on the PreToolUse payload at dispatch,
    not on the emitted event). We return an empty dict in that case;
    the formatter falls back to ``???`` which is the documented behaviour.
    """
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return {}
    ti = payload.get("tool_input")
    return ti if isinstance(ti, dict) else {}


def _format_tool_action(tool_name: str, tool_input: dict) -> str:
    """Render a tool invocation per the LOCKED §5.6 table.

    Preserves tool identity even when the payload carries no meaningful
    target: renders a bare ``<tool_name>`` rather than the
    broken-looking ``<tool_name> → ???`` (F-V3). The ``→`` arrow is only
    shown when there is an actual target/argument to point at.
    """
    if not tool_name:
        # Truly unknown event shape — no identity to preserve.
        return "(unknown tool)"

    ti = tool_input if isinstance(tool_input, dict) else {}

    if tool_name == "Bash":
        cmd = str(ti.get("command") or "")
        if not cmd:
            return tool_name
        shown = cmd[:60] + ("…" if len(cmd) > 60 else "")
        return f'{tool_name} → run("{shown}")'

    if tool_name in ("Edit", "Write", "NotebookEdit", "Read"):
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if not path:
            return tool_name
        return f'{tool_name} → "{path}"'

    if tool_name == "Grep":
        pattern = ti.get("pattern") or ""
        path = ti.get("path") or ""
        if pattern and path:
            return f'{tool_name} → "{pattern}" in "{path}"'
        if pattern:
            return f'{tool_name} → "{pattern}"'
        return tool_name

    if tool_name == "Glob":
        pattern = ti.get("pattern") or ""
        return f'{tool_name} → "{pattern}"' if pattern else tool_name

    if tool_name == "WebFetch":
        url = ti.get("url") or ""
        return f'{tool_name} → {url}' if url else tool_name

    if tool_name == "WebSearch":
        query = ti.get("query") or ""
        return f'{tool_name} → "{query}"' if query else tool_name

    if tool_name == "ToolSearch":
        # Claude Code's ToolSearch payload carries no query/target today
        # (it surfaces as a bare SCOPE_ASSERTED with no searchable arg),
        # so this renders as a clean "ToolSearch". Future-proof: if a
        # query field appears, show it.
        query = ti.get("query") or ti.get("q") or ""
        return f'{tool_name} → "{query}"' if query else tool_name

    if tool_name in ("Agent", "Task"):
        desc = ti.get("description") or ti.get("prompt") or ""
        if desc:
            shown = str(desc)[:60] + ("…" if len(str(desc)) > 60 else "")
            return f'{tool_name} → {shown}'
        return tool_name

    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) >= 3:
            _, server, op = parts
            return f"MCP[{server}] → {op}(...)"
        return tool_name

    # Unknown tool — preserve identity, degrade payload to ???.
    return tool_name


def _format_anomaly_action(anomaly: "_EventAnomaly") -> str:
    """Render one anomaly's action line for Key Events / Focus refs.

    SCOPE_ASSERTED events render via the tool-action path (preserves
    tool identity via :func:`_format_tool_action`). Non-SCOPE events
    (MEMORY_WRITE_ATTEMPT, INTENT_DECLARED, CONTEXT_SNAPSHOT) render
    via :func:`_format_event_oneline` so operators see a meaningful
    narrative instead of ``??? → ???``.
    """
    if anomaly.event_type == "SCOPE_ASSERTED":
        return _format_tool_action(anomaly.tool_name, anomaly.tool_input or {})
    if anomaly.raw_event:
        return _format_event_oneline(anomaly.raw_event)
    return "(unknown event)"


def _format_nontool_event(event: dict) -> str:
    """Render a non-tool event as narrative per §5.6, not schema.

    Examples:
        INTENT_DECLARED → none provided
        CONTEXT_SNAPSHOT → unclassified context (44 tokens)
        AGENT_REGISTERED → agent claude-code-abc123
        MEMORY_WRITE_ATTEMPT → write to filesystem
    """
    event_type = event.get("event_type", "???")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

    if event_type == "AGENT_REGISTERED":
        agent_id = payload.get("agent_id") or event.get("agent_id") or "unknown"
        return f"{event_type} → agent {agent_id}"

    if event_type == "INTENT_DECLARED":
        source = payload.get("intent_source") or "none"
        if source in (None, "none", "", "unknown"):
            return f"{event_type} → none provided"
        objective = payload.get("stated_objective")
        if objective:
            shown = str(objective)[:50] + ("…" if len(str(objective)) > 50 else "")
            return f'{event_type} → "{shown}" (source={source})'
        return f"{event_type} → source={source}"

    if event_type == "CONTEXT_SNAPSHOT":
        tokens = payload.get("context_size_tokens")
        classification = payload.get("classification_source") or "unclassified"
        if tokens is not None:
            return f"{event_type} → {classification} context ({tokens} tokens)"
        return f"{event_type} → {classification} context"

    if event_type == "MEMORY_WRITE_ATTEMPT":
        target = payload.get("target_store") or "unknown target"
        return f"{event_type} → write to {target}"

    if event_type == "GOVERNANCE_ERROR":
        err = payload.get("error_type") or "unknown"
        return f"{event_type} → {err}"

    return f"{event_type} → ???"


def _format_event_oneline(event: dict) -> str:
    """Render any event as a single indented narrative line.

    For SCOPE_ASSERTED: ``<primitive> → <tool> → <action>``.
    For everything else: delegated to :func:`_format_nontool_event`.
    """
    event_type = event.get("event_type", "???")
    if event_type == "SCOPE_ASSERTED":
        tool_name = _extract_tool_name(event)
        tool_input = _extract_tool_input(event)
        action = _format_tool_action(tool_name, tool_input)
        # action already contains `<tool> → <payload>`; prefix with primitive.
        return f"{event_type} → {action}"
    return _format_nontool_event(event)


# ---------------------------------------------------------------------------
# Focus derivation — §5.3, MUST derive from Summary's anomaly list
# (§5.2 coupling rule). Never compute anomalies twice.
# ---------------------------------------------------------------------------


@dataclass
class _FocusBullet:
    marker: str  # always "⚠" per §5.3
    description: str  # plain-English ("write operations outside declared scope")
    refs: List[str]  # event-formatted references


def _build_focus_bullets(cls: _ClassifiedSession) -> List[_FocusBullet]:
    """Group anomalies into up to 4 user-impact-ordered bullets.

    Grouping strategy:
      1. Destructive ops (rank 1) -> "N write operations outside declared scope"
      2. Unexpected commands (rank 2) -> "N unexpected command executions"
      3. Scope mismatches (rank 3) -> "N scope mismatches"
      4. Context/escalation (rank 4) -> "N context escalations"
      Everything else (rank 5) -> "N other anomalies"

    Each bullet's ``refs`` holds up to 3 concrete event-formatted
    references for inline display (§5.3 example shape).
    """
    if not cls.anomalies:
        return []

    groups: Dict[int, List[_EventAnomaly]] = defaultdict(list)
    for a in cls.anomalies:
        groups[a.user_impact_rank].append(a)

    rank_to_label = {
        1: "write operations outside declared scope",
        2: "unexpected command executions",
        3: "scope mismatches",
        4: "context escalations",
        5: "other anomalies",
    }

    bullets: List[_FocusBullet] = []
    for rank in sorted(groups.keys()):
        group = groups[rank]
        label = rank_to_label[rank]
        refs = []
        for a in group[:3]:
            refs.append(_format_anomaly_action(a))
        bullets.append(
            _FocusBullet(
                marker="⚠",
                description=f"{len(group)} {label}",
                refs=refs,
            )
        )

    return bullets[:4]


# ---------------------------------------------------------------------------
# Render — the six-block output of `sentience open`.
# ---------------------------------------------------------------------------


def _render_header(session_id: str, events: List[dict]) -> str:
    first_ts = ""
    for ev in events:
        ts = ev.get("timestamp_utc") or ev.get("timestamp") or ""
        if ts:
            first_ts = ts.split(".")[0].replace("T", " ")
            break
    return (
        f"Session: {session_id}\n"
        f"Time:    {first_ts or 'unknown'}\n"
        f"Events:  {len(events)}\n"
    )


def _render_summary(cls: _ClassifiedSession) -> str:
    if cls.anomalies:
        status_line = "Status: ⚠ anomalies detected\n"
        lines = [status_line, ""]
        # FIX-3 (v0.2.8) propagation to the open summary (F19(b)): never
        # an advisory count under a "Violations" label — split policy
        # violations from advisory flags, mirroring `status`/`list`.
        pol, adv = _split_anomaly_counts(cls)
        lines.append(f"⚠ Policy violations: {pol}")
        lines.append(f"  Advisory flags:    {adv}")
        for code, n in cls.anomaly_code_counts.most_common():
            g = _gloss(code)
            suffix = f" — {g}" if g else ""
            lines.append(f"  - {n} {code}{suffix}")
    else:
        lines = [
            "Status: ✓ baseline behavior",
            "",
            "No violations beyond expected Claude Code baseline.",
        ]

    lines.append("")
    # F19(a) (v0.2.9): self-label what is counted so a reader knows this is
    # tool-call frequency (SCOPE_ASSERTED events), not the event total. Full
    # events→violations→tool-calls ledger reconciliation is deferred to the
    # v0.3.3 local SQLite store.
    total_tool_calls = sum(cls.tool_counts.values())
    lines.append(f"Tool calls observed: {total_tool_calls}")
    lines.append("Top tools by SCOPE_ASSERTED count:")
    top = cls.tool_counts.most_common(5)
    if top:
        for tool, n in top:
            lines.append(f"  {tool} ({n})")
    else:
        lines.append("  (none)")
    lines.append("")
    return "Summary\n\n" + "\n".join(lines)


def _render_focus(cls: _ClassifiedSession) -> str:
    header = "Focus (what to pay attention to)\n\n"
    if not cls.anomalies:
        return header + "No anomalies detected. Observed flags match expected Claude Code baseline behavior.\n"
    bullets = _build_focus_bullets(cls)
    body_lines: List[str] = []
    for b in bullets:
        refs_text = ""
        if b.refs:
            refs_text = " (" + "; ".join(b.refs) + ")"
        body_lines.append(f"• {b.marker} {b.description}{refs_text}")
    return header + "\n".join(body_lines) + "\n"


def _render_notes(cls: _ClassifiedSession) -> str:
    """Shown only when at least one baseline code crossed the threshold."""
    if not cls.baseline_codes_present:
        return ""
    lines = ["Notes", ""]
    if "INTENT_MISSING" in cls.baseline_codes_present:
        lines.append("- INTENT_MISSING is expected (Claude Code does not expose intent yet)")
    if ("POL-003" in cls.baseline_codes_present
            or "CONTEXT_UNCLASSIFIED" in cls.baseline_codes_present):
        lines.append("- POL-003 appears on most context reads (current classification limitation)")
    lines.append("- Focus on:")
    lines.append("    • scope mismatches")
    lines.append("    • write operations")
    lines.append("    • unexpected tool usage")
    lines.append("")
    return "\n".join(lines)


def _render_key_events(cls: _ClassifiedSession) -> str:
    header = "Key Events\n\n"
    if not cls.anomalies:
        return header + "No violations detected in this session.\n"

    ordered = sorted(
        cls.anomalies,
        key=lambda a: (a.user_impact_rank, a.sequence),
    )
    shown = ordered[:10]
    more = len(ordered) - len(shown)

    blocks: List[str] = []
    for a in shown:
        action = _format_anomaly_action(a)
        gloss = _gloss(a.code)
        issue = f"{a.code} ({gloss})" if gloss else a.code
        blocks.append(f"⚠ [{a.sequence}] {action}\n    Issue: {issue}")
    out = "\n\n".join(blocks) + "\n"
    if more > 0:
        out += f"\n... and {more} more (see Full Trace)\n"
    return header + out


def _render_full_trace(events: List[dict]) -> str:
    header = "Full Trace\n\n"
    lines: List[str] = []
    for ev in events:
        seq = ev.get("event_sequence_number", "?")
        line = _format_event_oneline(ev)
        lines.append(f"[{seq}] {line}")
    return header + "\n".join(lines) + "\n"


def _render_footer(trace_dir: Path, session_file: Optional[Path]) -> str:
    raw_path = str(session_file) if session_file else str(trace_dir)
    return (
        "Next steps\n\n"
        "View all sessions → sentience list\n"
        "Open another      → sentience open <session_id>\n"
        f"Raw JSONL         → cat {raw_path}\n"
        "\n"
        "Tip: after running Claude Code, use `sentience open --latest` to review the session.\n"
    )


def render_open(
    session_id: str,
    events: List[dict],
    session_file: Path,
    summary: bool = False,
) -> str:
    """Compose the output for one session.

    ``summary=False`` (default): six-block output — Header, Summary,
    Focus, Notes, Key Events, Full Trace, Footer.

    ``summary=True``: skips the Full Trace block. Fits on one terminal
    screen for the typical session. Every anomaly still surfaces in
    Key Events; the JSONL file on disk is unchanged. The footer
    already tells operators where to read the raw events if they
    need them.

    Note: this is not a filter on event content. The Full Trace
    invariant ("every event rendered in order") holds on the default
    output; ``--summary`` only controls whether that block is
    printed to the terminal.
    """
    cls = classify_session(events)
    blocks = [
        _render_header(session_id, events),
        _render_summary(cls),
        _render_focus(cls),
        _render_notes(cls),
        _render_key_events(cls),
    ]
    if not summary:
        blocks.append(_render_full_trace(events))
    blocks.append(_render_footer(session_file.parent, session_file))
    # Notes block is empty for clean sessions — elide it cleanly.
    return "\n".join(b for b in blocks if b)


# ---------------------------------------------------------------------------
# Session discovery — shared by `list`, `open --latest`, `open <id>`.
# ---------------------------------------------------------------------------


def _session_start_epoch(path: Path) -> Optional[float]:
    """Epoch seconds of a session's first event, or None if unavailable.

    The first event's ``timestamp_utc`` is the session-start time. Unlike
    file mtime, it is STABLE: it does not advance as the session appends
    more events. Used as the primary ordering key so an actively-written
    live session does not keep jumping to the top between consecutive CLI
    invocations (F-V4).

    Defensive — returns None on any read/parse failure so the caller can
    fall back to mtime.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            line = f.readline()
        if not line.strip():
            return None
        ts = json.loads(line).get("timestamp_utc")
        if not ts:
            return None
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _list_session_files(trace_dir: Path) -> List[Path]:
    """Return .jsonl session files in the directory, newest first.

    Ordering key is ``(session_start_time, mtime)`` descending:
      - **Primary: session-start time** (first event's timestamp_utc).
        Stable across invocations — a live session being appended does
        not reorder, so `list`, `open --latest`, and `analyze --latest`
        always agree on which session is "latest" (F-V4).
      - **Secondary: mtime** — tiebreaker when start times are equal or
        unavailable (e.g. fixtures sharing a timestamp, or malformed
        first events). Falls back to pure-mtime behavior in those cases.

    Excludes the ``.jsonl.index`` sidecar glob automatically (Path.glob
    suffix match is strict).
    """
    if not trace_dir.exists() or not trace_dir.is_dir():
        return []
    files = [p for p in trace_dir.glob("*.jsonl") if p.is_file()]

    def _key(p: Path) -> Tuple[float, float]:
        mtime = p.stat().st_mtime
        start = _session_start_epoch(p)
        return (start if start is not None else mtime, mtime)

    files.sort(key=_key, reverse=True)
    return files


def _load_session(path: Path) -> Tuple[str, List[dict]]:
    """Load one session file, return (session_id, events).

    Uses the shared ``parse_events`` so NDJSON / JSON-array parsing
    stays consistent with ``sentience-cli``. If the file contains
    multiple session_ids (shared-file mode), returns only the
    session_id matching the filename stem, falling back to the first
    session_id encountered.
    """
    with open(path, encoding="utf-8") as fh:
        sessions = parse_events(fh)
    if not sessions:
        return (path.stem, [])
    if path.stem in sessions:
        return (path.stem, sessions[path.stem])
    # Shared-file or stem mismatch — pick the first session encountered.
    first = next(iter(sessions))
    return (first, sessions[first])


def _is_transient_bootstrap(events: List[dict]) -> bool:
    """v0.3.0.4 — the confirmed transient-bootstrap (ghost) signature.

    True iff the session is EXACTLY the artifact a Claude Code app restart
    leaves behind when ``SessionEnd`` is the first-and-only hook invocation
    for a transient session that never did anything: two events —
    ``AGENT_REGISTERED`` followed by ``INTENT_DECLARED`` with
    ``intent_source == "none"`` and a null ``stated_objective`` — and
    nothing else.

    Deliberately narrow. A session carrying a real declared intent
    (non-null objective / non-"none" source), or ANY further event, is not
    transient. Empty, single-event, or malformed sessions are never
    classified transient — they keep whatever behavior they have today.
    """
    if len(events) != 2:
        return False
    first_ev, second_ev = events
    if not isinstance(first_ev, dict) or not isinstance(second_ev, dict):
        return False
    if first_ev.get("event_type") != "AGENT_REGISTERED":
        return False
    if second_ev.get("event_type") != "INTENT_DECLARED":
        return False
    payload = second_ev.get("payload") or {}
    return (
        payload.get("intent_source") == "none"
        and payload.get("stated_objective") is None
    )


# ---------------------------------------------------------------------------
# F18 (v0.2.8.1) — "latest session with token data" hint.
#
# The desktop app mints a new session id on every conversation resume, so the
# newest-by-start-time session is often a tiny live segment with no token-
# bearing turns — shadowing the ended session that actually has token data.
# On an empty-state report, name the most recent token-bearing session so the
# operator can `sentience pulse <id>` it. Read-only and latest-preserving:
# it NAMES the richer session, it does not switch to it (D9/D12 intact).
# ---------------------------------------------------------------------------

def _session_token_turn_count(path: Path) -> int:
    """Number of distinct token-bearing turns in a session file.

    A turn is a unique ``llm_turn_id`` on a CONTEXT_SNAPSHOT. Reads the file
    once; tolerant of malformed lines (returns 0 on any read failure).
    """
    try:
        _sid, events = _load_session(path)
    except (OSError, ValueError):
        return 0
    turn_ids = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event_type") != "CONTEXT_SNAPSHOT":
            continue
        payload = ev.get("payload") or {}
        tid = payload.get("llm_turn_id")
        if tid:
            turn_ids.add(tid)
    return len(turn_ids)


def _latest_token_bearing_session(
    trace_dir: Path, exclude_session_id: str, max_probe: int = 25,
) -> Optional[Tuple[str, int]]:
    """Most recent session (newest-first) WITH token-bearing turns, excluding
    the current one. Returns ``(session_id, turn_count)`` or ``None``. Bounded
    to ``max_probe`` files to cap cost on large trace dirs; stops at first hit.
    """
    if not trace_dir.exists():
        return None
    for path in _list_session_files(trace_dir)[:max_probe]:
        sid = path.stem
        if sid == exclude_session_id:
            continue
        n = _session_token_turn_count(path)
        if n > 0:
            return sid, n
    return None


def _token_bearing_hint(trace_dir: Path, exclude_session_id: str) -> Optional[str]:
    """F18: one-line hint naming the most recent token-bearing session, or
    ``None`` if there is no other token-bearing session to point at."""
    found = _latest_token_bearing_session(trace_dir, exclude_session_id)
    if found is None:
        return None
    sid, n = found
    return (
        f"\nLatest session with token data: {sid} "
        f"({n:,} turns) — run `sentience pulse {sid}`."
    )


def _relative_time(mtime: float, now: Optional[float] = None) -> str:
    now = now or time.time()
    delta = max(0, now - mtime)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    if delta < 7 * 86400:
        return f"{int(delta / 86400)}d ago"
    # Over a week — show absolute date.
    return time.strftime("%Y-%m-%d", time.localtime(mtime))


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def run_status(args: argparse.Namespace) -> int:
    """`sentience status` — is the hook capturing sessions?"""
    trace_dir = _resolve_trace_dir()

    if not trace_dir.exists():
        # KG-1 (v0.2.8.1): honour --json on the empty-state early returns —
        # a --json flag that prints human text is a contract bug.
        if getattr(args, "json", False):
            print(json.dumps(
                {"hook": "not detected", "trace_path": str(trace_dir),
                 "last_session": None},
                indent=2, sort_keys=True,
            ))
            return 1
        print("Sentience Status\n")
        print(f"Hook:           not detected")
        print(f"Trace path:     {trace_dir}")
        print()
        print("No sessions found yet.")
        print()
        print("Run Claude Code with the hook enabled, then run:")
        print("    sentience status")
        return 1

    files = _list_session_files(trace_dir)
    if not files:
        # KG-1 (v0.2.8.1): same JSON contract on the no-sessions path.
        if getattr(args, "json", False):
            print(json.dumps(
                {"hook": "trace path available", "trace_path": str(trace_dir),
                 "last_session": None},
                indent=2, sort_keys=True,
            ))
            return 1
        print("Sentience Status\n")
        print(f"Hook:           trace path available")
        print(f"Trace path:     {trace_dir}")
        print()
        print("No sessions captured yet.")
        print()
        print("Run Claude Code with the hook enabled, then run:")
        print("    sentience status")
        return 1

    # At least one session exists. v0.3.0.4: "Last session" is the newest
    # session that is NOT a transient bootstrap (ghost) artifact. The scan
    # is exhaustive — every candidate is loaded and classified, newest
    # first, until the first non-transient session or the set is exhausted.
    # No cap and no heuristic: any shortcut that could misclassify would
    # let a ghost displace the operator's real latest session again.
    selected = None
    newest_transient = None
    transient_ids: List[str] = []
    for candidate in files:
        cand_sid, cand_events = _load_session(candidate)
        if _is_transient_bootstrap(cand_events):
            if newest_transient is None:
                newest_transient = (candidate, cand_sid, cand_events)
            transient_ids.append(cand_sid)
            continue
        selected = (candidate, cand_sid, cand_events)
        break

    if selected is not None:
        latest, session_id, events = selected
        displayed_transient = False
        # Every transient passed over on the way to the real session.
        skipped_ids = transient_ids
    else:
        # All candidates are transient: display the newest transient; the
        # remaining N-1 are the skipped ones. The displayed session never
        # appears in skipped_ids.
        latest, session_id, events = newest_transient
        displayed_transient = True
        skipped_ids = transient_ids[1:]

    # FIX-3 (v0.2.8): split policy violations from advisory flags —
    # never an advisory count under a "Violations" label (§4.3).
    cls = classify_session(events)
    pol_total, adv_total = _split_anomaly_counts(cls)

    ts = latest.stat().st_mtime
    ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    # `status --json` — the machine-readable audit path (FIX-3): raw vs
    # displayed counts reconcile without re-deriving from trace files.
    if getattr(args, "json", False):
        payload = {
            "hook": "sessions detected",
            "trace_path": str(trace_dir),
            "last_session": {
                "id": session_id,
                "time": ts_str,
                "events": len(events),
                **_status_reconciliation(events, cls),
            },
            # v0.3.0.4 (additive): exactly the transient sessions that were
            # passed over and are NOT displayed. The displayed last_session
            # id never appears in ids.
            "transient_sessions": {
                "skipped": len(skipped_ids),
                "ids": skipped_ids,
            },
        }
        if displayed_transient:
            # Only in the all-transient edge case; absence means false.
            payload["last_session"]["transient"] = True
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    # Pick a sample event for the status — prefer a SCOPE_ASSERTED so
    # the reader sees a tool invocation; fall back to any event.
    sample_line = ""
    for ev in events:
        if ev.get("event_type") == "SCOPE_ASSERTED":
            tool_name = _extract_tool_name(ev)
            sample_line = _format_tool_action(tool_name, _extract_tool_input(ev))
            break
    if not sample_line and events:
        sample_line = _format_event_oneline(events[0])

    print("Sentience Status\n")
    print(f"Hook:           sessions detected")
    print(f"Trace path:     {trace_dir}")
    print()
    if displayed_transient:
        print("Last session (transient — no recorded activity):")
    else:
        print("Last session:")
    print(f"  ID:                 {session_id}")
    print(f"  Time:               {ts_str}")
    print(f"  Events:             {len(events)}")
    print(f"  Policy violations:  {pol_total}")
    print(f"  Advisory flags:     {adv_total}")
    if skipped_ids:
        print()
        print(
            f"Note: skipped {len(skipped_ids)} transient session(s) "
            "with no recorded activity."
        )
    if sample_line:
        print()
        print("Sample event:")
        print(f"  {sample_line}")
    print()
    print("Sentience is governing your Claude Code sessions locally.")
    return 0


def run_list(args: argparse.Namespace) -> int:
    """`sentience list` — what sessions exist?"""
    trace_dir = _resolve_trace_dir()
    files = _list_session_files(trace_dir)
    if not files:
        print("No sessions found yet.")
        print()
        print("Run Claude Code with the hook enabled, then run:")
        print("    sentience status")
        return 0

    print("Sentience Sessions\n")

    shown = files[:20]
    for i, f in enumerate(shown, start=1):
        session_id, events = _load_session(f)
        # v0.3.0.4: residual transient-bootstrap (ghost) sessions stay
        # visible — append-only transparency — but are labeled as what the
        # evidence proves instead of carrying a vacuous ✓/⚠ verdict cell.
        if _is_transient_bootstrap(events):
            rel = _relative_time(f.stat().st_mtime)
            sid_short = session_id[:12] if session_id else f.stem[:12]
            print(
                f"{i:>2}. {sid_short:<16} {rel:<8} "
                f"{len(events):>3} events   transient — no activity"
            )
            continue
        cls = classify_session(events)
        # FIX-3 (v0.2.8): split glyph — violations and advisories are
        # different categories and must read as such (e.g. "⚠ 26v/52a").
        pol_total, adv_total = _split_anomaly_counts(cls)
        symbol = "⚠" if (pol_total + adv_total) > 0 else "✓"
        rel = _relative_time(f.stat().st_mtime)
        # Short session id for display (first 12 chars).
        sid_short = session_id[:12] if session_id else f.stem[:12]
        print(
            f"{i:>2}. {sid_short:<16} {rel:<8} "
            f"{len(events):>3} events   {symbol} {pol_total}v/{adv_total}a"
        )

    if len(files) > 20:
        print()
        print(f"Showing latest 20 sessions (of {len(files)} total)")

    print()
    print("Tip:")
    print("Open latest session → sentience open --latest")
    print("Open specific      → sentience open <session_id>")
    return 0


def run_open(args: argparse.Namespace) -> int:
    """`sentience open [--latest | <session_id> | <path>]` — render one session.

    Accepts a session-id prefix, a trace file path, or --latest — the
    same forms `sentience analyze` accepts (F-V10), via the shared
    resolver.
    """
    target, err = _resolve_session_target(args.session_id, args.latest)
    if target is None:
        print(err, file=sys.stderr)
        return 1

    session_id, events = _load_session(target)
    if not events:
        print(f"Session file {target} has no parseable events.", file=sys.stderr)
        return 1

    summary_mode = getattr(args, "summary", False)
    out = render_open(session_id, events, target, summary=summary_mode)
    print(out)
    return 0


# ---------------------------------------------------------------------------
# Analyze subcommand — undeclared-intent
# ---------------------------------------------------------------------------


_REPORTS_DIR = Path.home() / ".sentience" / "reports"


def _bundled_showcase_path() -> Optional[Path]:
    """Return the path to the bundled closed-loop showcase trace, or None.

    Ships inside the package (``sentience_governor/data/showcase/``) so a
    fresh-install operator can see a populated analysis via
    ``sentience analyze undeclared-intent --showcase`` before they have
    wired token capture (F-V5). The package installs as regular files
    (not zip-imported), so the resource path is directly usable.
    """
    try:
        from importlib import resources

        ref = resources.files("sentience_governor").joinpath(
            "data/showcase/v025-closed-loop.jsonl"
        )
        p = Path(str(ref))
        return p if p.is_file() else None
    except (ModuleNotFoundError, AttributeError, OSError, TypeError):
        return None


def _resolve_session_target(
    target: Optional[str],
    latest: bool,
) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a session reference to a trace file path.

    Shared by `sentience open` AND `sentience analyze` so both
    subcommands accept the same forms (F-V10 — previously `open` only
    accepted session-id prefixes, while `analyze` also accepted file
    paths).

    Returns ``(path, error_message)``. On success ``error_message`` is
    None; on failure ``path`` is None.

    Resolution order:
      1. ``target`` is an existing file path → use it directly.
      2. ``target`` looks like a path (contains '/' or .jsonl suffix)
         but does not exist → clear file-not-found error (not treated
         as a session prefix).
      3. ``latest`` (or no target) → newest session in the trace dir.
      4. Otherwise treat ``target`` as a session-id prefix.
    """
    # File-path mode (does not depend on the trace dir).
    if target:
        candidate = Path(target).expanduser()
        if candidate.is_file():
            return candidate, None
        if "/" in target or candidate.suffix == ".jsonl":
            return None, f"Trace file not found: {target}"

    trace_dir = _resolve_trace_dir()
    files = _list_session_files(trace_dir)
    if not files:
        return None, (
            f"No sessions found under {trace_dir}.\n"
            "Run a Claude Code session with the hook enabled, "
            "or pass an explicit trace file path."
        )

    if latest or not target:
        return files[0], None

    # Session-id prefix match.
    exact = [f for f in files if f.stem == target]
    if exact:
        return exact[0], None
    prefix = [f for f in files if f.stem.startswith(target)]
    if len(prefix) == 1:
        return prefix[0], None
    if len(prefix) > 1:
        names = "\n".join(f"  {f.stem}" for f in prefix[:10])
        return None, (
            f"Ambiguous session prefix {target!r} — {len(prefix)} matches:\n"
            f"{names}"
        )
    return None, f"No session matching {target!r}"


def _resolve_analyze_target(
    args: argparse.Namespace,
) -> Tuple[Optional[Path], Optional[str]]:
    """Thin wrapper over :func:`_resolve_session_target` for analyze."""
    return _resolve_session_target(
        getattr(args, "target", None),
        getattr(args, "latest", False),
    )


def _write_undeclared_report(result: Dict, target_path: Path) -> Path:
    """Write the saved Markdown report and return its path.

    Path shape:
      ~/.sentience/reports/undeclared-intent-<sid-prefix>-<timestamp>.md

    Side-effecting: creates the reports directory if needed.
    """
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sid = (result.get("session_id") or target_path.stem) or "unknown"
    sid_short = sid[:12].replace("/", "_")
    ts = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    out = _REPORTS_DIR / f"undeclared-intent-{sid_short}-{ts}.md"
    body = render_undeclared_markdown(result)
    out.write_text(body, encoding="utf-8")
    return out


def run_analyze_undeclared_intent(args: argparse.Namespace) -> int:
    """`sentience analyze undeclared-intent [target] [flags]` handler.

    Per plan v3 §"Save prompt — behavior contract":
      - Save prompt fires only after successful render AND status==ok.
      - --json / --no-prompt suppress prompting unconditionally.
      - --save skips the prompt and writes directly.
      - On non-ok status (no_token_data / no_turns / partial), the
        save prompt is suppressed.
      - After a successful save, echo the written file path.
    """
    if getattr(args, "showcase", False):
        target_path = _bundled_showcase_path()
        if target_path is None:
            print(
                "error: bundled showcase trace not found in this install.",
                file=sys.stderr,
            )
            return 1
    else:
        target_path, err = _resolve_analyze_target(args)
        if target_path is None:
            print(err, file=sys.stderr)
            return 1

    try:
        _, events = _load_session(target_path)
    except (OSError, ValueError) as exc:
        print(f"Failed to read trace {target_path}: {exc}", file=sys.stderr)
        return 1

    result = compute_undeclared_intent_spend(events)

    # --json mode: emit structured result and exit, no prompts.
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=False))
        return 0

    # Human-readable render.
    print(render_undeclared_cli(result))

    # F18 (v0.2.8.1): empty-state hint pointing at the token-bearing session.
    if result.get("status") in ("no_token_data", "no_turns"):
        hint = _token_bearing_hint(
            _resolve_trace_dir(), result.get("session_id") or ""
        )
        if hint:
            print(hint)

    # Save flow — strict P7 ordering.
    status = result.get("status")
    save_eligible = status == "ok"  # partial / no_token_data / no_turns are NOT eligible

    if getattr(args, "save", False):
        if not save_eligible:
            print(
                f"Skipping save: status={status} (only status=ok results are saved).",
                file=sys.stderr,
            )
            return 0
        out_path = _write_undeclared_report(result, target_path)
        print(f"Saved to {out_path}")
        return 0

    if getattr(args, "no_prompt", False):
        return 0

    if not save_eligible:
        return 0

    # Interactive prompt — only fires for status=ok and only after the
    # metric has already been printed above (P7 ordering).
    try:
        answer = input("Save this report? [Y/n]: ").strip().lower()
    except EOFError:
        # Non-interactive stdin — treat as decline; do not save.
        return 0
    if answer in ("", "y", "yes"):
        out_path = _write_undeclared_report(result, target_path)
        print(f"Saved to {out_path}")
    return 0


# ---------------------------------------------------------------------------
# v0.2.6 CP3 — `sentience analyze policy-violations` handler + writer
# ---------------------------------------------------------------------------
#
# Mirrors the undeclared-intent shape directly:
#   - Save prompt fires only after successful render AND status in
#     (ok, no_violations) — both surface-eligible for save.
#   - --json / --no-prompt suppress prompting unconditionally.
#   - --save skips the prompt and writes directly.
#   - On non-eligible status (no_token_data / no_turns / partial), the
#     save prompt is suppressed (v0.2.4 contract preserved for the
#     standalone analyzer; pulse-level save eligibility lives in CP6).
#   - After a successful save, echo the written file path.


def _write_burn_rate_report(result: Dict, target_path: Path) -> Path:
    """Write the saved Markdown report and return its path.

    Path shape:
      ~/.sentience/reports/policy-violations-<sid-prefix>-<timestamp>.md

    Side-effecting: creates the reports directory if needed.
    """
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sid = (result.get("session_id") or target_path.stem) or "unknown"
    sid_short = sid[:12].replace("/", "_")
    ts = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    out = _REPORTS_DIR / f"policy-violations-{sid_short}-{ts}.md"
    body = render_burn_rate_markdown(result)
    out.write_text(body, encoding="utf-8")
    return out


def run_analyze_policy_violations(args: argparse.Namespace) -> int:
    """`sentience analyze policy-violations [target] [flags]` handler.

    Per plan v3.6 CP3 spec + v3.5 fix #8 (CLI test tightening):
      - Save prompt fires only after successful render AND status in
        (ok, no_violations) — both are surface-eligible save states
        for the standalone analyzer.
      - --json / --no-prompt suppress prompting unconditionally.
      - --save skips the prompt and writes directly.
      - On non-eligible status (no_token_data / no_turns / partial),
        the save prompt is suppressed (standalone analyzer keeps the
        v0.2.4 skip-save-on-non-ok contract — distinct from pulse).
      - After a successful save, echo the written file path.
    """
    if getattr(args, "showcase", False):
        target_path = _bundled_showcase_path()
        if target_path is None:
            print(
                "error: bundled showcase trace not found in this install.",
                file=sys.stderr,
            )
            return 1
    else:
        target_path, err = _resolve_analyze_target(args)
        if target_path is None:
            print(err, file=sys.stderr)
            return 1

    try:
        _, events = _load_session(target_path)
    except (OSError, ValueError) as exc:
        print(f"Failed to read trace {target_path}: {exc}", file=sys.stderr)
        return 1

    result = compute_policy_violation_burn_rate(events)

    # --json mode: emit structured result and exit, no prompts.
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=False))
        return 0

    # Human-readable render.
    print(render_burn_rate_cli(result))

    # F18 (v0.2.8.1): empty-state hint pointing at the token-bearing session.
    if result.get("status") in ("no_token_data", "no_turns"):
        hint = _token_bearing_hint(
            _resolve_trace_dir(), result.get("session_id") or ""
        )
        if hint:
            print(hint)

    # Save eligibility — standalone analyzer contract.
    # Both `ok` (violations + tokens) and `no_violations` (clean session
    # with tokens) are save-eligible. The clean-session report is
    # explicitly designed to be shareable; suppressing save would
    # contradict the v3 plan's "no_violations feels useful, not empty"
    # acceptance criterion.
    status = result.get("status")
    save_eligible = status in ("ok", "no_violations")

    if getattr(args, "save", False):
        if not save_eligible:
            print(
                f"Skipping save: status={status} (only status=ok or "
                "no_violations results are saved).",
                file=sys.stderr,
            )
            return 0
        out_path = _write_burn_rate_report(result, target_path)
        print(f"Saved to {out_path}")
        return 0

    if getattr(args, "no_prompt", False):
        return 0

    if not save_eligible:
        return 0

    # Interactive prompt — only fires for eligible status and only
    # after the metric has already been printed above (P7 ordering).
    try:
        answer = input("Save this report? [Y/n]: ").strip().lower()
    except EOFError:
        # Non-interactive stdin — treat as decline; do not save.
        return 0
    if answer in ("", "y", "yes"):
        out_path = _write_burn_rate_report(result, target_path)
        print(f"Saved to {out_path}")
    return 0


# ---------------------------------------------------------------------------
# v0.2.6 CP6 — `sentience pulse` handler + sync-prompt eligibility
# ---------------------------------------------------------------------------
#
# Pulse is the v0.2.6 top-level adoption surface. It lives alongside
# `status`, `list`, `open`, `analyze`, `profile` at the top of the
# subparser tree — NOT nested under `analyze`, per plan v3.6 F6
# ("Operators don't think 'let me run an analyzer that's a meta-
# analyzer.' They think 'let me see my session pulse.'").
#
# Three-layer purity discipline (plan v3.6 §"sync_prompt field
# discipline"; footer signal repointed in v0.2.8.3):
#   1. compute_pulse(events) is pure; ships default sync_prompt =
#      {"show": False, "reason": "uninitialized"}.
#   2. run_pulse(args) (this module) reads ~/.sentience/first-run.json
#      (the launch-list / email-list state) and SENTIENCE_NO_SYNC_PROMPT,
#      computes eligibility, then overwrites result["sync_prompt"]
#      before rendering.
#   3. render_pulse_cli / render_pulse_markdown (renderers.py) read
#      result.sync_prompt.show ONLY — never touch disk or env.
#
# v0.2.8.3 sunset note (recorded inline so future readers see it):
#   The pulse footer is the "Sentience Sync" EMAIL-LIST CTA
#   (getsentience.ai/sentience-sync), NOT the removed Sync cloud
#   telemetry. Its show/hide used to read that former telemetry state;
#   that telemetry was sunset in v0.2.8.3, so the signal now comes from
#   the launch-list first-run state (first_run.py writes
#   ~/.sentience/first-run.json with a durable "subscribed" flag). The
#   field key "sync_prompt" is kept for schema stability; it gates the
#   email footer, not any cloud sync.

# Launch-list / first-run state file: written by
# sentience_governor.cli.first_run (the email-list capture flow).
_FIRST_RUN_STATE_PATH = Path.home() / ".sentience" / "first-run.json"

# Opt-out env var — suppresses the email-list footer.
_SYNC_OPTOUT_ENV_VAR = "SENTIENCE_NO_SYNC_PROMPT"

# Override env var so tests (and operators with a relocated state dir)
# can point the footer-eligibility read at a specific first-run file.
_FIRST_RUN_STATE_PATH_ENV_VAR = "SENTIENCE_FIRST_RUN_STATE_PATH"


def _resolve_first_run_state_path() -> Path:
    """Resolve the launch-list first-run state file, honouring the env override.

    Defaults to ~/.sentience/first-run.json (what
    sentience_governor.cli.first_run writes). Test suites that
    monkeypatch SENTIENCE_FIRST_RUN_STATE_PATH get the expected override
    behaviour for free.
    """
    override = os.environ.get(_FIRST_RUN_STATE_PATH_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return _FIRST_RUN_STATE_PATH


def _determine_sync_prompt_eligibility() -> Dict[str, Any]:
    """Compute the {show, reason} dict for the pulse EMAIL-LIST footer.

    The footer is the "Sentience Sync" email-list CTA, not cloud
    telemetry (that was sunset in v0.2.8.3). Decision order (FIRST match
    wins):

    1. ``SENTIENCE_NO_SYNC_PROMPT=1`` set
       → ``{show: False, reason: "opted_out"}``. Operator-level
       suppression of the footer takes precedence.

    2. The launch-list first-run state exists AND parses AND has
       ``subscribed`` truthy
       → ``{show: False, reason: "already_subscribed"}``. The operator
       already joined the email list; don't nag.

    3. Otherwise (no file, unreadable, parse error, or not subscribed)
       → ``{show: True, reason: "not_subscribed"}``.

    Reads the launch-list signal (first-run.json) — never the removed
    cloud telemetry state. Side-effecting only via the env /
    filesystem reads the plan locates here on purpose (single source of
    truth for disk / env IO).
    """
    # (1) Operator-level opt-out takes precedence.
    if os.environ.get(_SYNC_OPTOUT_ENV_VAR, "").strip() == "1":
        return {"show": False, "reason": "opted_out"}

    # (2) Already joined the email list? Read first-run.json defensively
    # — any failure (missing file, permission error, malformed JSON, or
    # not subscribed) means "not subscribed", so the footer still shows.
    state_path = _resolve_first_run_state_path()
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # File doesn't exist, isn't readable, or doesn't parse → treat
        # as not subscribed.
        return {"show": True, "reason": "not_subscribed"}

    if isinstance(data, dict) and data.get("subscribed"):
        return {"show": False, "reason": "already_subscribed"}

    # (3) File present but not subscribed (they skipped the prompt) →
    # keep showing the CTA so a later change of mind is easy.
    return {"show": True, "reason": "not_subscribed"}


def _write_pulse_report(result: Dict, target_path: Path) -> Path:
    """Write the saved Markdown pulse report and return its path.

    Path shape (plan v3.6 §"Markdown report path"):
      ~/.sentience/reports/pulse-<sid-prefix>-<timestamp>.md

    Side-effecting: creates the reports directory if needed.
    """
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    sid = (result.get("session_id") or target_path.stem) or "unknown"
    sid_short = sid[:12].replace("/", "_")
    ts = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    out = _REPORTS_DIR / f"pulse-{sid_short}-{ts}.md"
    body = render_pulse_markdown(result)
    out.write_text(body, encoding="utf-8")
    return out


def run_pulse(args: argparse.Namespace) -> int:
    """`sentience pulse [target] [flags]` handler.

    Flow per plan v3.6 §CP6:
      1. Resolve the target trace (positional / --latest / --showcase).
      2. Load events.
      3. Call compute_pulse(events) — pure, deterministic.
      4. Call _determine_sync_prompt_eligibility() — single point of
         disk / env IO. Overwrites result["sync_prompt"].
      5. --json: emit serialized result and exit.
      6. --human: render to terminal, then save-prompt flow.

    Save eligibility — pulse-specific contract (plan v3.6 §"Save
    eligibility (pulse)"):
      ALL pulse statuses are save-eligible — ok / partial / limited /
      no_signal. A no_signal pulse is itself a useful artifact (it
      tells the reader the trace had no usable analyzer signal,
      which is information). The CLI handler MUST NOT skip save
      based on pulse status — this is a deliberate divergence from
      the standalone analyzer commands.

    --no-prompt semantics: suppresses interactive prompts only
    (specifically the "Save this pulse? [Y/n]" prompt). Does NOT
    suppress the email-list footer text — the footer is
    non-interactive Markdown, not a prompt. Footer suppression
    requires SENTIENCE_NO_SYNC_PROMPT=1.
    """
    if getattr(args, "showcase", False):
        target_path = _bundled_showcase_path()
        if target_path is None:
            print(
                "error: bundled showcase trace not found in this install.",
                file=sys.stderr,
            )
            return 1
    else:
        target_path, err = _resolve_analyze_target(args)
        if target_path is None:
            print(err, file=sys.stderr)
            return 1

    try:
        _, events = _load_session(target_path)
    except (OSError, ValueError) as exc:
        print(f"Failed to read trace {target_path}: {exc}", file=sys.stderr)
        return 1

    # (3) Pure analyzer composition.
    result = compute_pulse(events)

    # (4) Attach sync-prompt eligibility — single point of IO.
    result["sync_prompt"] = _determine_sync_prompt_eligibility()

    # (5) --json mode: emit structured result and exit, no prompts.
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=False))
        return 0

    # F20 (v0.2.8.2): when the resolved (latest) session has no usable signal,
    # default to the most recent session that DOES have token data. The live
    # session is empty by construction (the desktop app mints a new session id
    # on every resume), so "--latest" alone is useless live; "latest" here means
    # "latest with usable signal". Only on an implicit target — an explicit
    # `pulse <id>` is always honoured exactly (the escape hatch; D9 preserved).
    explicit_target = bool(getattr(args, "target", None))
    if (not explicit_target) and result.get("status") == "no_signal":
        fb = _latest_token_bearing_session(
            _resolve_trace_dir(), result.get("session_id") or ""
        )
        if fb is not None:
            fb_sid, fb_turns = fb
            fb_path = _resolve_trace_dir() / f"{fb_sid}.jsonl"
            try:
                _, fb_events = _load_session(fb_path)
                fb_result = compute_pulse(fb_events)
                fb_result["sync_prompt"] = result["sync_prompt"]
                print(
                    "The latest session has no per-turn token data yet "
                    "(it may still be running, or was a transient session).\n"
                    f"Showing the most recent session that does — {fb_sid} "
                    f"({fb_turns:,} turns).\n"
                    "Run `sentience pulse <id>` for a specific session.\n"
                )
                result, target_path = fb_result, fb_path
            except (OSError, ValueError):
                pass  # fall through to the empty render + F18 hint below

    # (6) Human render.
    print(render_pulse_cli(result))

    # F18 (v0.2.8.1): if STILL empty (no token-bearing session anywhere, or an
    # explicit empty target), name the most recent token-bearing session.
    if result.get("status") == "no_signal":
        hint = _token_bearing_hint(
            _resolve_trace_dir(), result.get("session_id") or ""
        )
        if hint:
            print(hint)

    # --save: write directly, no prompt. All statuses eligible.
    if getattr(args, "save", False):
        out_path = _write_pulse_report(result, target_path)
        print(f"Saved to {out_path}")
        return 0

    # --no-prompt suppresses the save prompt only. The footer text
    # in the rendered output above is unaffected (it's not a prompt).
    if getattr(args, "no_prompt", False):
        return 0

    # Interactive save prompt — fires for ALL pulse statuses (the
    # pulse-specific contract that diverges from the standalone
    # analyzers).
    try:
        answer = input("Save this pulse? [Y/n]: ").strip().lower()
    except EOFError:
        return 0
    if answer in ("", "y", "yes"):
        out_path = _write_pulse_report(result, target_path)
        print(f"Saved to {out_path}")
    return 0


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v0.2.5 — `sentience profile ...` subcommand handlers
# ---------------------------------------------------------------------------
#
# Six verbs (per plan §CLI proposal):
#   * view      — print the active profile (defaults or file-backed)
#   * validate  — read-only schema check; never mutates the file
#   * export    — write current profile to an explicit path
#   * import    — read from explicit path, validate, install at default
#   * edit      — open the default profile file in $EDITOR
#   * init      — create a starter profile at the default path
#
# Each handler returns the process exit code (0 on success, non-zero on
# error). All output is written via print() to stdout/stderr; no
# handler ever raises (errors are translated to printed messages +
# non-zero exit).


def _read_profile_for_view(
    explicit_path: Optional[Path] = None,
) -> Tuple[GovernanceProfile, bool]:
    """Load a profile for ``view`` / ``validate``.

    Returns ``(profile, was_loaded_from_file)``. When ``explicit_path``
    is given, the profile is loaded from that path; otherwise the
    default path is consulted. ``was_loaded_from_file`` is False only
    when no file existed at the default path — useful for ``view`` to
    tell the operator "you're seeing defaults, not your file."
    """
    if explicit_path is not None:
        return GovernanceProfile.from_file(explicit_path), True
    if not DEFAULT_PROFILE_PATH.is_file():
        return GovernanceProfile.defaults(), False
    return GovernanceProfile.from_file(DEFAULT_PROFILE_PATH), True


def run_profile_view(args: argparse.Namespace) -> int:
    """Print the active profile to stdout.

    When no profile file exists, prints the defaults with a banner so
    the operator understands they're seeing the fallback, not their
    own configuration. ``--resolved`` is reserved for showing
    effective values after future inheritance (``extends``); in v0.2.5
    it behaves the same as the default view.
    """
    try:
        profile, from_file = _read_profile_for_view()
    except (ValueError, OSError) as exc:
        print(f"error: failed to load profile: {exc}", file=sys.stderr)
        return 1

    if not from_file:
        print(
            f"# No profile file found at {DEFAULT_PROFILE_PATH}.",
            file=sys.stderr,
        )
        print(
            "# Showing defaults. Run `sentience profile init` to create one.",
            file=sys.stderr,
        )
    else:
        print(f"# Source: {profile.source_path}", file=sys.stderr)
        print(f"# Fingerprint: {profile.fingerprint()}", file=sys.stderr)

    # YAML body to stdout — operators can pipe `sentience profile view`
    # straight into a file if they want a copy.
    try:
        import yaml  # local import; PyYAML is a v0.2.5 dependency
    except ImportError:
        print("error: PyYAML is required to render profiles", file=sys.stderr)
        return 1
    print(
        yaml.safe_dump(
            profile.to_dict(),
            default_flow_style=False,
            sort_keys=False,
            indent=2,
        ),
        end="",
    )
    return 0


def run_profile_validate(args: argparse.Namespace) -> int:
    """Validate the profile at the default path (or an explicit one).

    READ-ONLY: never writes to disk. Prints validation result and a
    content-hash integrity check against the file's header (if
    present). Exit 0 when valid, 1 on errors.
    """
    target_path: Optional[Path] = (
        Path(args.path) if getattr(args, "path", None) else None
    )

    try:
        profile, from_file = _read_profile_for_view(explicit_path=target_path)
    except (ValueError, OSError) as exc:
        print(f"error: failed to load profile: {exc}", file=sys.stderr)
        return 1

    if not from_file:
        print(
            f"No profile file at {DEFAULT_PROFILE_PATH}. "
            "Defaults are valid by construction.",
            file=sys.stderr,
        )
        return 0

    result = profile.validate(strict=bool(getattr(args, "strict", False)))
    print(result.format_human())

    # Integrity check: compare computed hash to header (if present).
    # Strictly read-only — we only peek the first few lines.
    header_hash = _peek_header_hash(profile.source_path)
    if header_hash is not None:
        current = profile.content_hash()
        if header_hash == current:
            print(f"content_hash: OK ({current[:12]}...)")
        else:
            # F-V9: this is informational, not an error — the operator
            # edited the file after it was generated, which is expected
            # and fine. The old "MISMATCH" wording read as a failure to
            # non-developers. The runtime always uses the recomputed
            # hash; the header is advisory and never modified here.
            print(
                f"ℹ Header hash is stale — file edited after generation. "
                f"The runtime uses the recomputed hash {current[:12]} "
                f"(header was {header_hash[:12]})."
            )

    if not result.is_valid:
        return 1
    return 0


def run_profile_export(args: argparse.Namespace) -> int:
    """Write the active profile to an explicit path with a fresh header."""
    dest = Path(args.path).expanduser()
    try:
        profile, _ = _read_profile_for_view()
        profile.export(dest)
    except (ValueError, OSError) as exc:
        print(f"error: export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Exported profile to {dest}")
    return 0


def run_profile_import(args: argparse.Namespace) -> int:
    """Read a profile from an explicit path, validate, install at default.

    Errors out if validation fails so operators can't accidentally
    install a broken file. The destination is overwritten on success
    (operator is expected to back up first if needed).
    """
    src = Path(args.path).expanduser()
    if not src.is_file():
        print(f"error: source file not found: {src}", file=sys.stderr)
        return 1

    try:
        profile = GovernanceProfile.from_file(src)
    except (ValueError, OSError) as exc:
        print(f"error: failed to read {src}: {exc}", file=sys.stderr)
        return 1

    result = profile.validate(strict=False)
    if not result.is_valid:
        print("Import refused — profile failed validation:", file=sys.stderr)
        print(result.format_human(), file=sys.stderr)
        return 1

    try:
        profile.export(DEFAULT_PROFILE_PATH)
    except OSError as exc:
        print(f"error: failed to install profile: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {src} -> {DEFAULT_PROFILE_PATH}")
    return 0


def _resolve_editor_command() -> Optional[List[str]]:
    """Resolve an editor launch command, or None if none is available.

    F-V8: macOS does not set ``$EDITOR`` by default, and a non-developer
    operator is unlikely to have configured it. Rather than hard-error,
    fall back through a sensible chain:
      1. ``$VISUAL``
      2. ``$EDITOR``
      3. ``nano`` → ``vim`` → ``vi`` (whichever is on PATH)
      4. macOS only: ``open -e`` (TextEdit)

    Returns the command as a list prefix (so multi-arg launchers like
    ``open -e`` work), to which the file path is appended by the caller.
    """
    import shutil

    for env_var in ("VISUAL", "EDITOR"):
        val = os.environ.get(env_var, "").strip()
        if val:
            return [val]

    for editor in ("nano", "vim", "vi"):
        if shutil.which(editor):
            return [editor]

    if sys.platform == "darwin" and shutil.which("open"):
        return ["open", "-e"]

    return None


def run_profile_edit(args: argparse.Namespace) -> int:
    """Open the default profile file in an editor.

    Does NOT create the file; that's ``init``'s job (single
    responsibility). If the file doesn't exist, prints a hint and
    exits non-zero so operators don't silently end up editing an
    empty buffer.
    """
    if not DEFAULT_PROFILE_PATH.is_file():
        print(
            f"error: no profile at {DEFAULT_PROFILE_PATH}. "
            "Run `sentience profile init` first.",
            file=sys.stderr,
        )
        return 1

    editor_cmd = _resolve_editor_command()
    if editor_cmd is None:
        print(
            "error: no editor found. Set $EDITOR (e.g. `export EDITOR=nano`) "
            "or install nano/vim, then try again.",
            file=sys.stderr,
        )
        return 1

    import subprocess  # local import; only used here
    try:
        result = subprocess.run([*editor_cmd, str(DEFAULT_PROFILE_PATH)])
    except (FileNotFoundError, OSError) as exc:
        print(
            f"error: failed to launch editor {' '.join(editor_cmd)!r}: {exc}",
            file=sys.stderr,
        )
        return 1
    return result.returncode


def run_profile_init(args: argparse.Namespace) -> int:
    """Create a starter profile at the default path.

    Refuses to overwrite an existing file (operator must delete or
    move the old file first — never silent loss).
    """
    if DEFAULT_PROFILE_PATH.exists():
        print(
            f"error: {DEFAULT_PROFILE_PATH} already exists. "
            "Delete or move it before running init.",
            file=sys.stderr,
        )
        return 1

    profile = GovernanceProfile.defaults()
    try:
        profile.export(DEFAULT_PROFILE_PATH)
    except OSError as exc:
        print(f"error: failed to write {DEFAULT_PROFILE_PATH}: {exc}", file=sys.stderr)
        return 1
    print(f"Created starter profile at {DEFAULT_PROFILE_PATH}")
    print("Run `sentience profile view` to see it, or `sentience profile edit` to tune it.")
    return 0


def _peek_header_hash(path: Path) -> Optional[str]:
    """Return the SHA256 hash from a profile file's header, or None.

    Reads only the first few lines (header lives at the top); never
    parses or modifies the YAML body. Defensive — returns None on any
    failure rather than raising.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            for _ in range(10):  # header is at most a few lines
                line = f.readline()
                if not line:
                    return None
                # Expected shape: "# Content hash: sha256:<64 hex>"
                stripped = line.strip()
                if stripped.startswith("# Content hash:"):
                    _, _, value = stripped.partition(":")
                    # value is now "sha256:<hex>" with possible leading space
                    value = value.strip()
                    if value.startswith("sha256:"):
                        return value[len("sha256:") :]
                    return value
    except OSError:
        return None
    return None


_HOOK_BINARY_NAME = "sentience-claude-code-hook"
# v0.3.0 — the opt-in MCP server console script (registered by
# `sentience init claude-code --mcp`).
_MCP_SERVER_BINARY_NAME = "sentience-mcp-server"


def _resolve_sibling_binary(name: str) -> Optional[str]:
    """Return the absolute path to a console-script ``name`` shipped with
    this install, or None.

    Resolution order, designed to work across install methods
    (pipx-isolated venv, pip-in-venv, dev source):
      1. The directory containing the running interpreter
         (``sys.executable``) — this is the binary that belongs to the
         SAME install as the ``sentience`` binary being run. Preferred
         first because ``shutil.which`` can otherwise pick a *different*
         install that happens to be earlier on PATH (e.g. a global pipx
         install shadowing a project venv), which would wire a binary of
         the wrong version. Checks both POSIX (``bin/``) and Windows
         (``Scripts/`` next to ``python.exe``, ``.exe`` suffix) layouts.
      2. ``shutil.which`` — fallback for unusual layouts where the binary
         is not a sibling of the interpreter but is on PATH.
    """
    import shutil

    # NB: do NOT .resolve() sys.executable — a venv/pipx `python` is a
    # symlink to the base interpreter, and resolving it would jump OUT of
    # the venv bin dir (where the sibling lives) into the base Python's bin
    # (where it does not), defeating this whole branch. Use the unresolved
    # parent: the bin dir the running script lives in.
    interp_dir = Path(sys.executable).parent
    for cand in (
        interp_dir / name,
        interp_dir / f"{name}.exe",  # Windows
    ):
        if cand.is_file():
            return str(cand)

    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())

    return None


def _resolve_hook_binary() -> Optional[str]:
    """Absolute path to the ``sentience-claude-code-hook`` binary, or None.
    See :func:`_resolve_sibling_binary` for the resolution rationale."""
    return _resolve_sibling_binary(_HOOK_BINARY_NAME)


def _resolve_mcp_server_binary() -> Optional[str]:
    """Absolute path to the ``sentience-mcp-server`` binary, or None. Same
    resolution as the hook binary (belongs to this install)."""
    return _resolve_sibling_binary(_MCP_SERVER_BINARY_NAME)


def _hook_entry(command: str) -> dict:
    """The canonical hook entry shape written into .claude/settings.json."""
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


def _entry_has_command(entries: list, command: str) -> bool:
    """True if any hook entry in ``entries`` already wires ``command``."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks", []):
            if isinstance(hook, dict) and hook.get("command") == command:
                return True
    return False


# ----------------------------------------------------------------------
# v0.2.7 — Claude Code slash-command skills (D1/D5/D8/D10).
# `sentience init claude-code` installs six bundled SKILL.md files
# alongside the hook wiring. Install is idempotent via a per-root
# sidecar manifest (.sentience-skills.json) that distinguishes a managed
# update from an operator hand-edit (D8 v2.1).
# ----------------------------------------------------------------------

_SKILLS_DIRNAME = "skills"
_SKILLS_MANIFEST_NAME = ".sentience-skills.json"
_SENTIENCE_BINARY_NAME = "sentience"


def _bundled_skills() -> Dict[str, str]:
    """Return ``{skill_name: SKILL.md text}`` for every bundled skill (R4)."""
    from importlib import resources

    root = resources.files("sentience_governor").joinpath("data/skills")
    out: Dict[str, str] = {}
    for entry in root.iterdir():
        skill_md = entry.joinpath("SKILL.md")
        if entry.is_dir() and skill_md.is_file():
            out[entry.name] = skill_md.read_text(encoding="utf-8")
    return dict(sorted(out.items()))


def _skill_hash(text: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("sentience-governor")
        except PackageNotFoundError:
            return "unknown"
    except Exception:
        return "unknown"


def _load_skills_manifest(skills_root: Path) -> dict:
    """Read the sidecar manifest; tolerant of absent/empty/corrupt file."""
    p = skills_root / _SKILLS_MANIFEST_NAME
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_skills_manifest(skills_root: Path, manifest: dict) -> None:
    skills_root.mkdir(parents=True, exist_ok=True)
    (skills_root / _SKILLS_MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _install_skills(skills_root: Path, *, force: bool) -> Dict[str, List[str]]:
    """Install bundled skills into ``skills_root`` per the D8 v2.1 manifest
    algorithm. Returns a summary bucketing skill names by action
    (installed / updated / current / preserved). Raises ``OSError`` on
    write failure — the caller is fail-open (R5).

    The manifest is the third datum that lets us tell a managed update
    (we installed the current bytes; the bundle moved on) apart from an
    operator hand-edit (current bytes differ from what we recorded). No
    skill is ever overwritten without that positive provenance, except
    under ``--force``.
    """
    from datetime import timezone

    bundled = _bundled_skills()
    manifest = _load_skills_manifest(skills_root)
    now = datetime.now(timezone.utc).isoformat()
    pkg_ver = _package_version()

    summary: Dict[str, List[str]] = {
        "installed": [], "updated": [], "current": [], "preserved": [],
    }

    def _record(name: str, h: str) -> None:
        manifest[name] = {
            "installed_hash": h,
            "package_version": pkg_ver,
            "installed_at": now,
        }

    def _write(name: str, text: str, h: str) -> None:
        target = skills_root / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _record(name, h)

    for name, text in bundled.items():
        bundled_hash = _skill_hash(text)
        target = skills_root / name / "SKILL.md"
        entry = manifest.get(name)
        if not target.exists():
            _write(name, text, bundled_hash)
            summary["installed"].append(name)
            continue
        current_hash = _skill_hash(target.read_text(encoding="utf-8"))
        if current_hash == bundled_hash:
            # No-op; adopt/refresh the manifest hash if absent or stale.
            if entry is None or entry.get("installed_hash") != bundled_hash:
                _record(name, bundled_hash)
            summary["current"].append(name)
        elif entry is not None and entry.get("installed_hash") == current_hash:
            # Managed update: we installed current; the bundle moved on.
            _write(name, text, bundled_hash)
            summary["updated"].append(name)
        elif force:
            _write(name, text, bundled_hash)
            summary["updated"].append(name)
        else:
            # Hand-edited or unmanaged (manifest missing/mismatched): keep.
            summary["preserved"].append(name)

    _write_skills_manifest(skills_root, manifest)
    return summary


def _probe_sentience_on_path() -> bool:
    """True if the ``sentience`` binary is resolvable on the current PATH (D10).

    The skills shell out to ``sentience``; if it isn't on PATH, every slash
    command fails silently at invoke time. ``shutil.which`` answers exactly
    that question without depending on a subcommand's exit code. (An earlier
    v0.2.7 build ran ``sentience --version`` here — but that flag did not
    exist until v0.2.7.1, so the probe always reported a false negative and
    warned on every correct install. Fixed by keying off ``which`` and by
    adding a real ``--version`` flag.)
    """
    import shutil

    return shutil.which(_SENTIENCE_BINARY_NAME) is not None


def _run_skill_install(args: argparse.Namespace, project_dir: Path) -> None:
    """Install/update the slash-command skills with D10 detection.

    Scope posture (v2.1): hooks are wired against the project path being
    initialized; skills install to the PERSONAL skills dir by default;
    ``--project`` installs them into the same project path's
    ``.claude/skills/``. Fail-open: a write failure warns to stderr and
    returns without raising, so already-wired hooks survive (R5).
    """
    project = getattr(args, "project", False)
    force = getattr(args, "force", False)
    if project:
        skills_root = project_dir / ".claude" / _SKILLS_DIRNAME
        scope = "project"
    else:
        skills_root = Path.home() / ".claude" / _SKILLS_DIRNAME
        scope = "personal"

    # D10 snapshot: did the skills dir exist BEFORE this run? Drives the
    # restart-or-not message (per Claude Code "Live change detection").
    existed_before = skills_root.exists()

    try:
        summary = _install_skills(skills_root, force=force)
    except OSError as exc:
        print(
            f"warning: could not install skills into {skills_root}: {exc}\n"
            "  Hooks are wired and working; skills were skipped. Fix the "
            "permission/path issue and re-run `sentience init claude-code`.",
            file=sys.stderr,
        )
        return

    print()
    print(f"Skills ({scope}) -> {skills_root}")
    for action in ("installed", "updated", "current"):
        for name in summary[action]:
            print(f"  {action:<9} {name}")
    for name in summary["preserved"]:
        print(f"  preserved {name} (hand-edited; pass --force to overwrite)")

    # D10 PATH probe — warn (do NOT fail) if `sentience` isn't resolvable;
    # every slash invocation would otherwise fail silently later.
    if not _probe_sentience_on_path():
        print(
            "warning: `sentience` is not resolvable on your PATH; slash "
            "commands will fail when invoked. Ensure the install bin "
            "directory (e.g. ~/.local/bin) is on your $PATH.",
            file=sys.stderr,
        )

    # D10 restart messaging — chosen from the pre-write snapshot.
    print()
    if not existed_before:
        print("Restart any open Claude Code session to see the new commands.")
    else:
        print(
            "New commands will appear in your Claude Code session within a "
            "few seconds; no restart needed."
        )


# ----------------------------------------------------------------------
# v0.3.0 — opt-in MCP server registration (`--mcp`). Writes/merges a
# project-scoped .mcp.json so Claude Code can spawn the Sentience MCP
# server (governance-as-tools). Opt-in ONLY: never registered by default.
# ----------------------------------------------------------------------

_MCP_JSON_NAME = ".mcp.json"
_MCP_SERVER_KEY = "sentience"


def _mcp_extra_installed() -> bool:
    """True if the optional ``mcp`` SDK is importable (needed to actually run
    the server; the console script itself ships regardless)."""
    import importlib.util

    return importlib.util.find_spec("mcp") is not None


def _print_mcp_consent_notice(
    mcp_path: Path, command: str, already: bool
) -> None:
    """The Sentience-specific consent notice (plan §5.1), shown at
    ``--mcp`` registration in addition to Claude Code's own permission
    prompt. No em dashes (operator copy convention)."""
    verb = "already registered" if already else "registered (opt-in)"
    print()
    print(f"Sentience MCP server {verb} in {mcp_path}")
    print(f"  command: {command}")
    print()
    print("What this enables, and what it does not:")
    print("  - Claude can call Sentience governance tools in this project.")
    print("  - Most tools are read-only (explain, profile, pulse, intent,")
    print("    violations, session status).")
    print("  - declare_intent appends a local declaration event to your")
    print("    trace (append-only); it never edits prior events.")
    print("  - No policy or profile mutation tools are installed.")
    print("  - No HTTP server is enabled (stdio only, local to this machine).")
    print("  - Token analysis is unavailable until a session ends (SessionEnd).")
    if not _mcp_extra_installed():
        # Context-aware: a pipx-managed install cannot be repaired by ambient
        # pip, so never print that command to a pipx user.
        from sentience_governor.mcp_server.install_hint import remediation_lines

        print()
        print("  Note: install the server dependency to run it:")
        for _cmd in remediation_lines(repair=False):
            print(f"    {_cmd}")


def _register_mcp_server(project_dir: Path) -> bool:
    """Register the Sentience MCP server (opt-in) in ``<project>/.mcp.json``
    and print the consent notice. Idempotent; never clobbers other servers.

    Fail-open (R5): a missing binary or unreadable/again-shaped .mcp.json
    warns to stderr and returns False so already-wired hooks survive.
    """
    command = _resolve_mcp_server_binary()
    if command is None:
        from sentience_governor.mcp_server.install_hint import remediation_lines

        _hint = "; ".join(remediation_lines(repair=False))
        print(
            "warning: could not locate the 'sentience-mcp-server' binary; "
            f"skipping MCP registration. Install the server dependency with: {_hint}",
            file=sys.stderr,
        )
        return False

    mcp_path = project_dir / _MCP_JSON_NAME
    config: dict = {}
    if mcp_path.is_file():
        try:
            raw = mcp_path.read_text(encoding="utf-8").strip()
            config = json.loads(raw) if raw else {}
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"warning: could not read {mcp_path}: {exc}; skipping MCP "
                "registration. Fix or remove it, then re-run with --mcp.",
                file=sys.stderr,
            )
            return False
        if not isinstance(config, dict):
            print(
                f"warning: {mcp_path} does not contain a JSON object; skipping "
                "MCP registration to avoid clobbering it.",
                file=sys.stderr,
            )
            return False

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(
            f"warning: 'mcpServers' in {mcp_path} is not an object; skipping "
            "MCP registration to avoid clobbering it.",
            file=sys.stderr,
        )
        return False

    existing = servers.get(_MCP_SERVER_KEY)
    already = isinstance(existing, dict) and existing.get("command") == command
    if not already:
        servers[_MCP_SERVER_KEY] = {"command": command, "args": []}
        try:
            mcp_path.write_text(
                json.dumps(config, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(
                f"warning: failed to write {mcp_path}: {exc}", file=sys.stderr
            )
            return False

    _print_mcp_consent_notice(mcp_path, command, already)
    return True


def run_init_claude_code(args: argparse.Namespace) -> int:
    """`sentience init claude-code [path]` — wire the Claude Code hook.

    Writes (or idempotently merges into) ``<path>/.claude/settings.json``
    so Claude Code invokes the sentience hook on every tool call. Does
    NOT clobber the operator's other hooks or settings.
    """
    project_dir = Path(getattr(args, "path", None) or ".").expanduser().resolve()
    if not project_dir.is_dir():
        print(f"error: {project_dir} is not a directory.", file=sys.stderr)
        return 1

    # v0.3.0.3 — one convergence engine (cli.hook_config). init is
    # converge(target, may_create_without_evidence=True): it creates the
    # canonical machine-local configuration in .claude/settings.local.json,
    # brings historical stale/partial/duplicate Sentience-managed
    # configuration forward, and treats the team-shared settings.json as
    # READ-ONLY migration evidence — it is never written.
    from sentience_governor.cli import hook_config as _hc

    res = _hc.converge(project_dir, caller="init")

    if res.outcome == _hc.NO_BINARY:
        print(
            f"error: could not locate the {_HOOK_BINARY_NAME!r} binary.\n"
            "It ships with sentience-governor — confirm the install "
            "(e.g. `pipx list | grep sentience-governor`) and that its "
            "bin directory exists.",
            file=sys.stderr,
        )
        return 1
    if res.outcome == _hc.BINARY_INVALID:
        print(
            f"error: the resolved hook binary is not executable: "
            f"{res.binary}",
            file=sys.stderr,
        )
        return 1
    if res.outcome in (_hc.UNREADABLE, _hc.MALFORMED):
        print(
            f"error: could not read existing settings ({res.reason}). "
            "Fix or remove the file, then re-run.",
            file=sys.stderr,
        )
        return 1
    if res.outcome == _hc.AMBIGUOUS_LOCAL:
        print(
            f"error: {res.local_path} contains a modified Sentience-looking "
            f"hook entry ({res.detail}). Sentience will not change it "
            "automatically.\nReview that entry: remove it, or restore it to "
            "the configuration Sentience generates. Then re-run.",
            file=sys.stderr,
        )
        return 1
    if res.outcome == _hc.SHARED_CONFLICT:
        print(
            f"Sentience: {res.shared_path} contains a live Sentience hook "
            "that differs\nfrom this install (or one Sentience cannot "
            "parse). Writing local configuration\ncould run two hooks per "
            "tool call.\nThat file is shared with your team, so Sentience "
            "will not change it.\nCoordinate its removal with your team, "
            "then re-run.",
            file=sys.stderr,
        )
        return 1
    if res.outcome in (_hc.UNWRITABLE, _hc.WRITE_CONFLICT):
        print(
            f"error: failed to write {res.local_path}: {res.reason}",
            file=sys.stderr,
        )
        return 1

    if res.outcome == _hc.NOOP:
        # Already canonical. NOT a total no-op: skills still refresh below.
        print(f"Sentience hook already current for {project_dir}")
    else:
        print(f"Sentience hook configured for {project_dir}")
        print(f"  file:     {res.local_path}   (machine-local; not for commit)")
        print(f"  command:  {res.binary}")
        print(f"  events:   {', '.join(_hc.GOVERNED_EVENTS)}")

    # v0.2.7: install the slash-command skills by default (D6); --no-skills
    # opts out. Runs AFTER hooks are wired so a skills failure leaves the
    # hooks intact (R5).
    if not getattr(args, "no_skills", False):
        _run_skill_install(args, project_dir)

    # v0.3.0: opt-in MCP server registration. Never registered unless the
    # operator passes --mcp; the default init is unchanged.
    if getattr(args, "mcp", False):
        _register_mcp_server(project_dir)

    print()
    print(f"Run Claude Code from {project_dir}, then: sentience status")
    return 0


def run_demo(args: argparse.Namespace) -> int:
    """`sentience demo <name>` — run a packaged, self-contained demo.

    Demos ship inside the package (F-V6) so they run from any install
    without the operator needing a Python path. Two demos:
      - undeclared-intent: a synthesized drift session (~20.7% undeclared)
      - closed-loop: the bundled clean showcase trace (100% declared)
    """
    name = getattr(args, "demo_name", None)

    if name == "undeclared-intent":
        from sentience_governor.demos import build_session_events

        events = build_session_events()
        result = compute_undeclared_intent_spend(events)
        print(render_undeclared_cli(result))
        print()
        print("This is a synthesized demo session. Re-run the analyzer on "
              "any captured trace with:")
        print("    sentience analyze undeclared-intent <session-id | path>")
        return 0

    if name == "closed-loop":
        path = _bundled_showcase_path()
        if path is None:
            print(
                "error: bundled showcase trace not found in this install.",
                file=sys.stderr,
            )
            return 1
        _, events = _load_session(path)
        result = compute_undeclared_intent_spend(events)
        print(render_undeclared_cli(result))
        return 0

    if name == "declare-intent":
        print(_render_declare_intent_flip())
        return 0

    # No demo name (shouldn't happen — argparse requires it) — list them.
    print("Available demos:")
    print("  sentience demo undeclared-intent   Synthesized drift session.")
    print("  sentience demo closed-loop         Clean closed-loop showcase.")
    print("  sentience demo declare-intent      declare_intent BEFORE/AFTER flip.")
    return 0


def _render_declare_intent_flip() -> str:
    """BEFORE/AFTER showcase of the POL-001 flip a mid-session declaration
    produces. The flip is computed by the real capture-time evaluator; the
    same analyzer runs on both traces (no analyzer change, no retrospective
    cleanup). Structural counts + em-dash-free copy."""
    from sentience_governor.demos.declare_intent_flip import (
        OBJECTIVE,
        SCOPE,
        run_flip_demo,
    )

    r = run_flip_demo()
    b, a = r.before_spend, r.after_spend

    def _pol(flags: List[bool]) -> str:
        return " ".join("POL-001" if f else "clean" for f in flags)

    lines: List[str] = []
    lines.append("declare_intent: BEFORE / AFTER (the POL-001 flip)")
    lines.append("")
    lines.append(
        "Same three mutating tool calls (Write -> filesystem), same synthetic "
        "token counts. The only difference is a mid-session declaration."
    )
    lines.append("")
    lines.append("BEFORE (no declaration):")
    lines.append(f"  mutating turns:      {_pol(r.before_scope_pol001)}")
    lines.append(
        f"  undeclared compute:  {b['undeclared_percent']}%  "
        f"({b['undeclared_tokens']}/{b['total_tokens']} tokens, "
        f"{b['undeclared_turn_count']}/{b['total_turn_count']} turns)"
    )
    lines.append("")
    lines.append(f'  ...declare_intent(objective="{OBJECTIVE}", scope={SCOPE})')
    lines.append("")
    lines.append("AFTER (declared after turn 1):")
    lines.append(
        f"  mutating turns:      {_pol(r.after_scope_pol001)}   "
        "(turn 1 = pre-declaration)"
    )
    lines.append(
        f"  undeclared compute:  {a['undeclared_percent']}%  "
        f"({a['undeclared_tokens']}/{a['total_tokens']} tokens, "
        f"{a['undeclared_turn_count']}/{a['total_turn_count']} turns)"
    )
    lines.append("")
    lines.append("What this shows:")
    lines.append(
        "  - Post-declaration matching activity stops firing POL-001; the "
        "declaration flips it from noise to signal."
    )
    lines.append(
        "  - The pre-declaration turn keeps its POL-001 (non-retroactive); "
        "the declaration is never applied backwards."
    )
    lines.append(
        "  - The flip happens at capture (the evaluator reads the declared "
        "baseline). No analyzer change, no rewriting of prior events."
    )
    return "\n".join(lines)


def run_explain(args: argparse.Namespace) -> int:
    """`sentience explain` — how Sentience counts (IR-5, v0.2.9).

    Methodology-only: token classes, the dedupe rule, the per-turn (not
    per-tool) attribution boundary, the operation-type enum, and the
    join-key semantics. Carries no session data. ``--json`` emits the same
    methodology as structured JSON for machine consumers (the MCP adapter
    consumes the identical dict).
    """
    from sentience_governor.analyze.methodology import build_methodology

    m = build_methodology()

    if getattr(args, "json", False):
        print(json.dumps(m, indent=2, sort_keys=True))
        return 0

    tc = m["token_classes"]
    lines = [
        "Sentience methodology — how the numbers are counted",
        "",
        "Token classes (per model turn; the four sum to total compute):",
        f"  prompt        {tc['prompt']}",
        f"  completion    {tc['completion']}",
        f"  cached read   {tc['cached_read']}",
        f"  cached write  {tc['cached_write']}",
        f"  {m['token_classes_note']}",
        "",
        "Dedupe:",
        f"  {m['dedupe_rule']}",
        "",
        "Attribution boundary:",
        f"  {m['attribution_boundary']}",
        "",
        "Operation types (SCOPE_ASSERTED.operation_type):",
        f"  {', '.join(m['operation_types'])}",
        f"  {m['operation_types_note']}",
        "",
        "Join keys:",
        f"  {m['join_keys']}",
    ]
    print("\n".join(lines))
    return 0


def _print_command_guide() -> int:
    """Friendly guide printed when `sentience` is run with no subcommand.

    F-V2: a first-time operator who types `sentience` to "see what's
    there" should get useful guidance, not an argparse error. argparse
    with required subcommands would exit non-zero before any of our
    code ran; instead we make the subcommand optional and route here.
    """
    print("Sentience Governor — local governance for agent sessions.")
    print()
    print("Commands:")
    print("  sentience scan              Review your existing Claude Code history.")
    print("  sentience status            Check the hook is capturing sessions.")
    print("  sentience list              List captured sessions, newest first.")
    print("  sentience open <id>         Render one session (Summary / Key Events / Trace).")
    print("  sentience pulse             One-command session report — composes the analyzers.")
    print("  sentience analyze <metric>  Run a single derived-metric analyzer over a session.")
    print("  sentience explain           Explain how Sentience counts (methodology).")
    print("  sentience profile <verb>    View / validate / edit the governance profile.")
    print("  sentience init claude-code  Wire the Claude Code hook into a project.")
    print()
    print("New here? Start with:")
    print("    sentience status     (confirm the hook is capturing)")
    print("    sentience pulse      (your one-command report, after a session)")
    print()
    print("Run any command with -h for details (e.g. `sentience analyze -h`).")
    return 0


def run_scan(args: argparse.Namespace) -> int:
    """`sentience scan` handler — the retrospective Reader (v0.3.1).

    Reader is a retrospective GTM/discovery surface, not runtime
    governance: it reads Claude Code's own history, which another system
    owns, and reports only what that evidence supports. It emits no
    GovernanceEvent, writes no trace, and mutates nothing.

    **The scan path performs zero writes of any kind.** `main()` bypasses
    both the first-run flow and the v0.3.0.3 convergence seam for this
    handler (see main()), so "local, transcripts read-only" carries no
    asterisk at the exact moment the product is asking to be trusted.
    """
    from sentience_governor import retro
    from sentience_governor.analyze.renderers import (
        render_scan, render_scan_detail,
    )

    result = retro.scan(since=getattr(args, "since", "all"))

    if getattr(args, "json", False):
        print(json.dumps(retro.json_payload(result), indent=2, sort_keys=False))
        return 0

    # Same scan, same window, same result — only the presentation differs.
    if getattr(args, "detail", False):
        print(render_scan_detail(result))
        return 0

    print(render_scan(result))
    return 0


def _bypasses_first_run(func) -> bool:
    """Whether the dispatched handler skips the interactive first-run flow.

    v0.3.1: `scan` is the cold-start GTM surface — on a fresh install the
    very first run must render its result with no email prompt and no
    outbound request. Identity comparison, never string matching.
    """
    return func is run_scan


def _bypasses_seam(func) -> bool:
    """Whether the dispatched handler skips the v0.3.0.3 convergence seam.

    `init claude-code` converges its own explicit target. `scan` is
    excluded because the Reader path performs zero writes of any kind
    (v0.3.1 §10.2): the seam can write `.claude/settings.local.json` in a
    project carrying Sentience evidence, and the trust statement should
    not need the qualification.
    """
    return func is run_init_claude_code or func is run_scan


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="sentience",
        description=(
            "Sentience Governor — curated viewer for agent-hook session traces "
            "(Claude Code today; more coming). Pairs with the raw "
            "`sentience-cli` tool for library-trace inspection."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"sentience {_package_version()}",
        help="Print the installed sentience-governor version and exit.",
    )

    # F-V2: subcommand is optional. Bare `sentience` routes to a helpful
    # guide (see end of main) instead of an argparse "required argument"
    # error. A bad subcommand still errors via argparse as before.
    subparsers = parser.add_subparsers(dest="command", required=False)

    p_status = subparsers.add_parser(
        "status", help="Check that the hook is capturing sessions."
    )
    p_status.add_argument(
        "--json",
        action="store_true",
        help=(
            "Structured output with the count-reconciliation fields "
            "(policy violations vs advisory flags vs baseline-filtered "
            "vs raw total)."
        ),
    )
    p_status.set_defaults(func=run_status)

    p_list = subparsers.add_parser(
        "list", help="List captured sessions, newest first (max 20)."
    )
    p_list.set_defaults(func=run_list)

    p_open = subparsers.add_parser(
        "open",
        help="Render one session with Summary / Focus / Notes / Key Events / Full Trace.",
    )
    p_open.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help=(
            "Session id (or prefix) under the default trace dir, OR a "
            "path to a .jsonl trace file. If omitted, uses --latest."
        ),
    )
    p_open.add_argument(
        "--latest",
        action="store_true",
        help="Open the most recently modified session (default when no session_id given).",
    )
    p_open.add_argument(
        "--summary",
        action="store_true",
        help=(
            "Skip the Full Trace block so the rendered output fits on "
            "one terminal screen. Header, Summary, Focus, Notes, Key "
            "Events, and Footer all print as usual. Every event is "
            "still on disk in the JSONL; the footer shows the path."
        ),
    )
    p_open.set_defaults(func=run_open)

    # ------------------------------------------------------------------
    # `sentience analyze ...` — derived metrics over v0.2.3+ traces.
    # First sub-subcommand: `undeclared-intent` (v0.2.4).
    # ------------------------------------------------------------------
    p_analyze = subparsers.add_parser(
        "analyze",
        help=(
            "Run a derived-metric analyzer over a captured session trace."
        ),
        # FIX-6 (v0.2.8): a bare `analyze <session-id>` errors with
        # argparse's invalid-choice message; the epilog shows the
        # correct shape so the error is self-recovering.
        epilog=(
            "A metric subcommand is required. Examples:\n"
            "    sentience analyze policy-violations <session-id>\n"
            "    sentience analyze undeclared-intent --latest"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    analyze_subparsers = p_analyze.add_subparsers(
        dest="analyzer", required=True
    )

    p_undeclared = analyze_subparsers.add_parser(
        "undeclared-intent",
        help=(
            "Compute how much compute was attributed to turns that "
            "touched execution outside the session's declared intent."
        ),
    )
    p_undeclared.add_argument(
        "target",
        nargs="?",
        default=None,
        help=(
            "Session id (or prefix) under the default trace dir, OR a "
            "path to an .jsonl trace file. If omitted, uses --latest."
        ),
    )
    p_undeclared.add_argument(
        "--latest",
        action="store_true",
        help="Analyze the most recently captured session.",
    )
    p_undeclared.add_argument(
        "--showcase",
        action="store_true",
        help=(
            "Analyze the bundled closed-loop showcase trace — a populated "
            "example you can run on a fresh install before wiring token "
            "capture. Ignores any target/--latest."
        ),
    )
    p_undeclared.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of human-readable output.",
    )
    p_undeclared.add_argument(
        "--save",
        action="store_true",
        help=(
            "Skip the interactive prompt and write the Markdown report "
            "directly to ~/.sentience/reports/."
        ),
    )
    p_undeclared.add_argument(
        "--no-prompt",
        action="store_true",
        dest="no_prompt",
        help="Disable the post-render save prompt entirely.",
    )
    p_undeclared.set_defaults(func=run_analyze_undeclared_intent)

    # ------------------------------------------------------------------
    # Second sub-subcommand: `policy-violations` (v0.2.6 CP3).
    # Mirrors the `undeclared-intent` shape; aggregates per-rule
    # token burn across turns where one or more POL rules fired.
    # ------------------------------------------------------------------
    p_burn_rate = analyze_subparsers.add_parser(
        "policy-violations",
        help=(
            "Compute compute associated with turns where policy rules "
            "fired. Aggregates per rule (POL-001 through POL-005)."
        ),
    )
    p_burn_rate.add_argument(
        "target",
        nargs="?",
        default=None,
        help=(
            "Session id (or prefix) under the default trace dir, OR a "
            "path to an .jsonl trace file. If omitted, uses --latest."
        ),
    )
    p_burn_rate.add_argument(
        "--latest",
        action="store_true",
        help="Analyze the most recently captured session.",
    )
    p_burn_rate.add_argument(
        "--showcase",
        action="store_true",
        help=(
            "Analyze the bundled closed-loop showcase trace — a clean "
            "session with zero policy violations, useful for confirming "
            "the renderer works on a fresh install. Ignores any "
            "target/--latest."
        ),
    )
    p_burn_rate.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of human-readable output.",
    )
    p_burn_rate.add_argument(
        "--save",
        action="store_true",
        help=(
            "Skip the interactive prompt and write the Markdown report "
            "directly to ~/.sentience/reports/."
        ),
    )
    p_burn_rate.add_argument(
        "--no-prompt",
        action="store_true",
        dest="no_prompt",
        help="Disable the post-render save prompt entirely.",
    )
    p_burn_rate.set_defaults(func=run_analyze_policy_violations)

    # ------------------------------------------------------------------
    # `sentience pulse [target]` — v0.2.6 CP6 top-level adoption surface.
    # Lives at the TOP level (alongside status / list / open / analyze /
    # profile), NOT under `analyze`, per plan v3.6 F6.
    # ------------------------------------------------------------------
    p_pulse = subparsers.add_parser(
        "pulse",
        help=(
            "One-command session pulse — composes undeclared-intent, "
            "policy-violation burn rate, and advisory-flag summary into "
            "a single shareable report."
        ),
    )
    p_pulse.add_argument(
        "target",
        nargs="?",
        default=None,
        help=(
            "Session id (or prefix) under the default trace dir, OR a "
            "path to an .jsonl trace file. If omitted, uses --latest."
        ),
    )
    p_pulse.add_argument(
        "--latest",
        action="store_true",
        help="Pulse the most recently captured session.",
    )
    p_pulse.add_argument(
        "--showcase",
        action="store_true",
        help=(
            "Pulse the bundled closed-loop showcase trace — useful for "
            "confirming pulse renders on a fresh install. Ignores any "
            "target/--latest."
        ),
    )
    p_pulse.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of human-readable output.",
    )
    p_pulse.add_argument(
        "--save",
        action="store_true",
        help=(
            "Skip the interactive prompt and write the Markdown pulse "
            "report directly to ~/.sentience/reports/."
        ),
    )
    p_pulse.add_argument(
        "--no-prompt",
        action="store_true",
        dest="no_prompt",
        help=(
            "Disable the interactive save prompt. Does NOT suppress the "
            "email-list footer (the footer is non-interactive "
            "Markdown; set SENTIENCE_NO_SYNC_PROMPT=1 to suppress it)."
        ),
    )
    p_pulse.set_defaults(func=run_pulse)

    # ------------------------------------------------------------------
    # `sentience explain` — IR-5 (v0.2.9): machine-readable methodology.
    # Methodology-only; no per-code mode in v0.2.9.
    # ------------------------------------------------------------------
    p_explain = subparsers.add_parser(
        "explain",
        help=(
            "Explain how Sentience counts — token classes, the dedupe "
            "rule, the per-turn (not per-tool) attribution boundary, the "
            "operation-type enum, and join-key semantics."
        ),
    )
    p_explain.add_argument(
        "--json",
        action="store_true",
        help="Emit the methodology as structured JSON.",
    )
    p_explain.set_defaults(func=run_explain)

    # ------------------------------------------------------------------
    # `sentience profile ...` — v0.2.5 governance profile commands.
    # ------------------------------------------------------------------
    p_profile = subparsers.add_parser(
        "profile",
        help=(
            "View, validate, and manage the operator-authored "
            "governance profile (~/.sentience/profile.yaml)."
        ),
    )
    profile_subparsers = p_profile.add_subparsers(
        dest="profile_verb", required=True
    )

    pv_view = profile_subparsers.add_parser(
        "view",
        help="Print the active profile (defaults if no file exists).",
    )
    pv_view.add_argument(
        "--resolved",
        action="store_true",
        help=(
            "Show effective values after resolving inheritance "
            "(reserved for future `extends`; v0.2.5 behaves the same "
            "as plain view)."
        ),
    )
    pv_view.set_defaults(func=run_profile_view)

    pv_validate = profile_subparsers.add_parser(
        "validate",
        help=(
            "Validate the profile schema. Read-only: never modifies "
            "the file."
        ),
    )
    pv_validate.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to validate (defaults to ~/.sentience/profile.yaml).",
    )
    pv_validate.add_argument(
        "--strict",
        action="store_true",
        help="Error on unknown keys instead of warning.",
    )
    pv_validate.set_defaults(func=run_profile_validate)

    pv_export = profile_subparsers.add_parser(
        "export",
        help="Write the active profile to an explicit path with a fresh header.",
    )
    pv_export.add_argument(
        "path",
        help="Destination file path.",
    )
    pv_export.set_defaults(func=run_profile_export)

    pv_import = profile_subparsers.add_parser(
        "import",
        help=(
            "Read a profile from a path, validate, install at "
            "~/.sentience/profile.yaml."
        ),
    )
    pv_import.add_argument(
        "path",
        help="Source file path.",
    )
    pv_import.set_defaults(func=run_profile_import)

    pv_edit = profile_subparsers.add_parser(
        "edit",
        help="Open ~/.sentience/profile.yaml in $EDITOR.",
    )
    pv_edit.set_defaults(func=run_profile_edit)

    pv_init = profile_subparsers.add_parser(
        "init",
        help="Create a starter profile at ~/.sentience/profile.yaml.",
    )
    pv_init.set_defaults(func=run_profile_init)

    # ------------------------------------------------------------------
    # `sentience init ...` — one-command runtime wiring.
    # First target: `claude-code` (writes/merges .claude/settings.json).
    # ------------------------------------------------------------------
    p_init = subparsers.add_parser(
        "init",
        help="Wire a runtime's hook so sentience captures its sessions.",
    )
    init_subparsers = p_init.add_subparsers(dest="runtime", required=True)

    pi_claude = init_subparsers.add_parser(
        "claude-code",
        help=(
            "Install the Claude Code hook into a project's machine-local "
            ".claude/settings.local.json (idempotent convergence). "
            "Requires Claude Code v2.1.211 or later."
        ),
    )
    pi_claude.add_argument(
        "path",
        nargs="?",
        default=None,
        help=(
            "Project directory to wire (defaults to the current "
            "directory). The hook is written to <path>/.claude/settings.json."
        ),
    )
    pi_claude.add_argument(
        "--no-skills",
        action="store_true",
        help=(
            "Wire hooks only; do not install the slash-command skills "
            "(/sentience-pulse, etc.)."
        ),
    )
    pi_claude.add_argument(
        "--project",
        action="store_true",
        help=(
            "Install skills into <path>/.claude/skills/ (shareable with a "
            "team via git) instead of the personal ~/.claude/skills/."
        ),
    )
    pi_claude.add_argument(
        "--force",
        action="store_true",
        help="Overwrite hand-edited or unmanaged skills during install.",
    )
    pi_claude.add_argument(
        "--mcp",
        action="store_true",
        help=(
            "Also register the Sentience MCP server (governance-as-tools) in "
            "<path>/.mcp.json so Claude can call it. Opt-in only; off by "
            'default. Needs the server extra: pip install '
            '"sentience-governor[mcp]".'
        ),
    )
    pi_claude.set_defaults(func=run_init_claude_code)

    # ------------------------------------------------------------------
    # `sentience demo ...` — packaged runnable demos (F-V6). Importable
    # from any install, so no Python-path knowledge needed.
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # `sentience scan` — v0.3.1 retrospective session reader.
    # The CLI name is implementation plumbing; the product surface is
    # "Sentience · Retrospective Review" (and `/sentience-review` in
    # Claude Code). This is the true pre-instrumentation cold-start
    # path: the command a stranger runs before adopting anything.
    # ------------------------------------------------------------------
    p_scan = subparsers.add_parser(
        "scan",
        help=(
            "Review your existing Claude Code history for project-boundary "
            "write activity. Local, read-only, no signup."
        ),
    )
    p_scan.add_argument(
        "--since",
        choices=["7d", "30d", "all"],
        default="all",
        help=(
            "Window to review, filtered on each record's own timestamp "
            "(never file mtime). Default: all — retrospective discovery of "
            "the whole history is the point; 7d/30d narrow it."
        ),
    )
    # Mutually exclusive by construction: `--detail` is new and carries no
    # compatibility burden, so an ambiguous invocation is refused with
    # standard argparse usage output rather than silently resolved.
    p_scan_mode = p_scan.add_mutually_exclusive_group()
    p_scan_mode.add_argument(
        "--detail",
        action="store_true",
        help=(
            "Show the evidence behind the retrospective review, grouped "
            "by session."
        ),
    )
    p_scan_mode.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured aggregate instead of the report.",
    )
    p_scan.set_defaults(func=run_scan)

    p_demo = subparsers.add_parser(
        "demo",
        help="Run a packaged demo session through the analyzer.",
    )
    p_demo.add_argument(
        "demo_name",
        choices=["undeclared-intent", "closed-loop", "declare-intent"],
        help=(
            "Which demo to run. 'undeclared-intent' = a synthesized drift "
            "session; 'closed-loop' = the bundled clean showcase trace; "
            "'declare-intent' = the BEFORE/AFTER POL-001 flip a mid-session "
            "declaration produces (v0.3.0)."
        ),
    )
    p_demo.set_defaults(func=run_demo)

    args = parser.parse_args()

    # First-run flow runs AFTER argparse succeeds (so --help / shell
    # completion paths exit before reaching here) and BEFORE the
    # requested subcommand executes. The flow itself short-circuits
    # if state already exists, if SENTIENCE_NO_FIRST_RUN_PROMPT is
    # set, or if any error occurs (defensive — never blocks a real
    # command). See sentience_governor/cli/first_run.py.
    # v0.3.1 — the Reader path is zero-write, and that includes the
    # first-run flow: on a fresh install the very first `sentience scan`
    # renders its result with no email prompt and no outbound request.
    # Identity comparison, matching the seam's `init claude-code`
    # exception style below.
    if not _bypasses_first_run(getattr(args, "func", None)):
        maybe_run_first_run_flow(package_version=_resolve_package_version())

    # F-V2: no subcommand given → print the guide (first-run flow has
    # already fired above, so a brand-new operator sees the welcome
    # AND the guide on a bare `sentience`).
    if getattr(args, "func", None) is None:
        return _print_command_guide()

    # v0.3.0.3 — the on-use convergence seam. Runs for every dispatched
    # subcommand EXCEPT `init claude-code`, which converges its own explicit
    # target (identity comparison, not string matching). Ordering: first-run
    # flow above (interactive), then convergence, then dispatch. Fail-open:
    # run_seam_convergence never raises, and it never configures a project
    # that carries no Sentience evidence.
    # v0.3.1 adds `scan` to the exception list: the seam can write
    # `.claude/settings.local.json` in a project carrying Sentience
    # evidence, and the Reader's trust posture is strongest as an
    # absolute — zero writes of any kind on the scan path. Cost: running
    # `scan` forgoes one opportunistic hook repair; any other command,
    # and the next session, still converge.
    if not _bypasses_seam(args.func):
        from sentience_governor.cli.hook_config import run_seam_convergence

        run_seam_convergence()

    return args.func(args)


def _resolve_package_version() -> Optional[str]:
    """Best-effort sentience-governor version, or None if unavailable.

    Used to populate the optional ``package_version`` field on the
    launch-list subscribe payload. If the package metadata isn't
    importable for any reason, return None — the field is optional
    and the server treats absence as no-op.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("sentience-governor")
    except (ImportError, Exception):  # PackageNotFoundError + safety
        return None


if __name__ == "__main__":
    sys.exit(main())
