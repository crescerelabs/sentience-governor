"""CLI Trace Viewer — reads newline-delimited JSON, renders governance traces.

Normative output format (from spec)
------------------------------------
SESSION: {session_id} | Agent: {agent_id} | {timestamp}
[{seq}] {PRIMITIVE}  {✓|⚠}  {summary}
  Policy violation: {rule_id}
  Consequence: {simulated_consequence}
  → Fix: {recommendation}
---
SESSION SUMMARY — {session_id}
...

Snapshot test: Flow B CLI output MUST match tests/snapshots/cli_flow_b_output.txt.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Dict, List, Optional, TextIO

# ---------------------------------------------------------------------------
# ANSI colour helpers (degrade gracefully if not a TTY)
# ---------------------------------------------------------------------------

def _supports_colour(stream: TextIO) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


class _C:
    RESET = "\033[0m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    DIM = "\033[2m"


def _colour(text: str, *codes: str, use_colour: bool = True) -> str:
    if not use_colour:
        return text
    return "".join(codes) + text + _C.RESET


# ---------------------------------------------------------------------------
# Fix recommendations per advisory flag
# ---------------------------------------------------------------------------

_FLAG_FIXES: Dict[str, str] = {
    "AGENT_UNREGISTERED": "Register agent before session start (POL-002)",
    "INTENT_MISSING": "Declare intent before executing mutating operations (POL-001)",
    "SCOPE_OPERATION_UNEXPECTED": "Declare intent before executing mutating operations (POL-001)",
    "SCOPE_INTENT_MISMATCH": "Ensure scope matches declared intent (POL-001)",
    "CONTEXT_UNCLASSIFIED": "Vendor should tag tool responses with classification metadata (POL-003)",
    "SENSITIVITY_ESCALATION": "Provide explicit authorization before escalating context sensitivity (POL-005)",
    "MEMORY_WRITE_UNCLASSIFIED": "Memory writes must include classification and retention policy (POL-004)",
    "MEMORY_WRITE_CANDIDATE": "Memory writes must include classification and retention policy (POL-004)",
}

_POL_FIXES: Dict[str, str] = {
    "POL-001": "Declare intent before executing mutating operations",
    "POL-002": "Register agent before session start",
    "POL-003": "Vendor should tag tool responses with classification metadata",
    "POL-004": "Memory writes must include classification and retention policy",
    "POL-005": "Provide explicit authorization before escalating context sensitivity",
}

# Intent signal quality based on intent_source
_INTENT_QUALITY: Dict[str, str] = {
    "explicit": "EXPLICIT — intent declared at session start",
    "inferred": "INFERRED — intent captured from agent inputs",
    "none": "MISSING — no intent signal detected",
}


def _event_summary(event: dict) -> str:
    etype = event.get("event_type", "UNKNOWN")
    payload = event.get("payload", {})

    if etype == "AGENT_REGISTERED":
        aid = payload.get("agent_id", "?")
        ver = payload.get("agent_version") or "no version"
        return f"Agent {aid} ({ver})"
    if etype == "INTENT_DECLARED":
        obj = payload.get("stated_objective") or "[none]"
        src = payload.get("intent_source", "?")
        return f"Objective: {obj!r}  source={src}"
    if etype == "SCOPE_ASSERTED":
        tool = payload.get("tool_id", "?")
        op = payload.get("operation_type", "?")
        sys_ = payload.get("target_system", "?")
        return f"{op} {tool} → {sys_}"
    if etype == "CONTEXT_SNAPSHOT":
        cls_ = payload.get("data_classifications") or []
        src = payload.get("classification_source", "?")
        tokens = payload.get("context_size_tokens", 0)
        cls_str = ", ".join(cls_) if cls_ else "unclassified"
        return f"classifications=[{cls_str}]  source={src}  tokens={tokens}"
    if etype == "MEMORY_WRITE_ATTEMPT":
        store = payload.get("target_store", "?")
        wtype = payload.get("write_type", "?")
        wcls = payload.get("write_classification", "?")
        ret = payload.get("retention_requested") or "null"
        return f"{wtype} → {store}  classification={wcls}  retention={ret}"
    if etype == "GOVERNANCE_ERROR":
        reason = payload.get("failure_reason", "?")
        sev = payload.get("severity", "?")
        return f"[{sev.upper()}] {reason}"
    return etype


def _primitive_label(event: dict) -> str:
    return event.get("primitive", event.get("event_type", "?"))


# ---------------------------------------------------------------------------
# Core renderer
# ---------------------------------------------------------------------------

def render_sessions(
    events_by_session: Dict[str, List[dict]],
    out: TextIO = sys.stdout,
    use_colour: Optional[bool] = None,
) -> None:
    if use_colour is None:
        use_colour = _supports_colour(out)

    for session_id, events in events_by_session.items():
        _render_session(session_id, events, out, use_colour)


def _render_session(
    session_id: str,
    events: List[dict],
    out: TextIO,
    use_colour: bool,
) -> None:
    # Sort by event_sequence_number; mark anomalies
    def sort_key(e: dict):
        seq = e.get("event_sequence_number")
        return seq if isinstance(seq, int) else 999999

    sorted_events = sorted(events, key=sort_key)
    seen_seqs: set = set()
    anomalies: List[int] = []
    for e in sorted_events:
        seq = e.get("event_sequence_number")
        if seq in seen_seqs:
            anomalies.append(seq)
        elif seq is not None:
            seen_seqs.add(seq)

    # Session header
    first = sorted_events[0] if sorted_events else {}
    agent_id = first.get("agent_id", "unknown")
    ts = first.get("timestamp_utc", "")
    header = f"SESSION: {session_id} | Agent: {agent_id} | {ts}"
    print(_colour(header, _C.BOLD, _C.CYAN, use_colour=use_colour), file=out)
    print(file=out)

    # Per-event lines
    total_blocked = 0
    total_mem_rejected = 0
    all_violations: List[str] = []
    intent_quality = "MISSING — no intent signal detected"

    for event in sorted_events:
        seq = event.get("event_sequence_number", "?")
        primitive = _primitive_label(event)
        flags = event.get("advisory_flags", [])
        violations = event.get("policy_violations", [])
        consequence = event.get("simulated_consequence")
        is_flagged = bool(flags or violations)

        if is_flagged:
            symbol = _colour("⚠", _C.YELLOW, use_colour=use_colour)
        else:
            symbol = _colour("✓", _C.GREEN, use_colour=use_colour)

        summary = _event_summary(event)

        # Detect sequence anomaly
        anom_note = ""
        if seq in anomalies:
            anom_note = _colour(" [SEQUENCE ANOMALY]", _C.RED, use_colour=use_colour)

        line = f"[{seq}] {primitive:<20}  {symbol}  {summary}{anom_note}"
        print(line, file=out)

        # Intent quality capture
        etype = event.get("event_type", "")
        if etype == "INTENT_DECLARED":
            src = event.get("payload", {}).get("intent_source", "none")
            intent_quality = _INTENT_QUALITY.get(src, f"UNKNOWN ({src})")

        # Flagged detail block
        if is_flagged:
            if violations:
                v_str = ", ".join(violations)
                print(
                    f"    Policy violation: {_colour(v_str, _C.RED, use_colour=use_colour)}",
                    file=out,
                )
                all_violations.extend(violations)
            if consequence:
                print(f"    Consequence: {consequence}", file=out)
            # Recommendations from flags
            shown_fixes: set = set()
            for flag in flags:
                fix = _FLAG_FIXES.get(flag)
                if fix and fix not in shown_fixes:
                    print(
                        f"    {_colour('→ Fix:', _C.CYAN, use_colour=use_colour)} {fix}",
                        file=out,
                    )
                    shown_fixes.add(fix)

        # Count blocked actions: REGISTRATION or SCOPE events with violations
        etype_for_count = event.get("event_type", "")
        if violations and etype_for_count in ("AGENT_REGISTERED", "SCOPE_ASSERTED"):
            total_blocked += 1

        # Count memory write rejections
        if etype_for_count == "MEMORY_WRITE_ATTEMPT" and violations:
            total_mem_rejected += 1

    # Session summary
    hr = "─" * 60
    print(file=out)
    print(_colour(hr, _C.DIM, use_colour=use_colour), file=out)
    summary_header = f"SESSION SUMMARY — {session_id}"
    print(_colour(summary_header, _C.BOLD, use_colour=use_colour), file=out)
    print(file=out)

    agent_line = f"Agent: {agent_id} | Topology: {first.get('deployment_mode', 'unknown')}"
    print(agent_line, file=out)
    print(file=out)

    unique_violations = sorted(set(all_violations))
    violation_str = f"{len(unique_violations)}  ({', '.join(unique_violations)})" if unique_violations else "0"

    print(f"Events intercepted:    {len(sorted_events)}", file=out)
    print(f"Policy violations:     {violation_str}", file=out)
    print(f"Actions that would have been blocked:    {total_blocked}", file=out)
    print(f"Memory writes that would have been rejected:    {total_mem_rejected}", file=out)
    print(f"Intent signal quality: {intent_quality}", file=out)
    print(file=out)

    # Recommendations
    shown_recs: set = set()
    for v in unique_violations:
        rec = _POL_FIXES.get(v)
        if rec and rec not in shown_recs:
            print(f"→ {rec} ({v})", file=out)
            shown_recs.add(rec)

    if not unique_violations:
        print(_colour("All events clean — no policy violations.", _C.GREEN, use_colour=use_colour), file=out)
    else:
        print(file=out)
        print(
            "Real-time enforcement of these policies is on the Sentience roadmap.",
            file=out,
        )

    print(file=out)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def parse_events(source: TextIO) -> Dict[str, List[dict]]:
    """Parse newline-delimited JSON or JSON array; group by session_id.

    Handles:
    * Newline-delimited JSON (one event per line) — primary format from sinks.
    * Pretty-printed or compact JSON arrays — for golden trace fixture files.
    * Malformed lines are skipped with a warning.
    """
    sessions: Dict[str, List[dict]] = defaultdict(list)
    content = source.read()

    # Detect JSON array format
    stripped = content.lstrip()
    if stripped.startswith("["):
        try:
            events = json.loads(content)
            if isinstance(events, list):
                for event in events:
                    if isinstance(event, dict):
                        session_id = event.get("session_id", "__unknown__")
                        sessions[session_id].append(event)
                return dict(sessions)
        except json.JSONDecodeError:
            pass  # fall through to line-by-line

    # Newline-delimited JSON
    for lineno, line in enumerate(content.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            print(
                f"WARNING: skipping malformed JSON on line {lineno}",
                file=sys.stderr,
            )
            continue
        session_id = event.get("session_id", "__unknown__")
        sessions[session_id].append(event)

    return dict(sessions)


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(
        prog="sentience-cli",
        description="Sentience Governor — CLI trace viewer",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help=(
            "Path to a .jsonl trace file, OR a directory of per-session "
            "trace files (default: stdin). "
            "Directories are scanned for *.jsonl and rendered in a single "
            "pass — convenient for the per-session trace layout the "
            "Claude Code hook uses by default "
            "(~/.sentience/traces/claude-code/)."
        ),
    )
    parser.add_argument(
        "--no-colour",
        action="store_true",
        help="Disable ANSI colour output",
    )
    args = parser.parse_args()

    sessions: Dict[str, List[dict]]
    if args.path:
        target = Path(args.path)
        if target.is_dir():
            sessions = _parse_events_from_directory(target)
        else:
            with open(target, encoding="utf-8") as fh:
                sessions = parse_events(fh)
    else:
        sessions = parse_events(sys.stdin)

    use_colour = None if not args.no_colour else False
    render_sessions(sessions, out=sys.stdout, use_colour=use_colour)


def _parse_events_from_directory(directory: "Path") -> Dict[str, List[dict]]:
    """Scan a directory for ``*.jsonl`` trace files and merge all sessions.

    Sidecar files (``*.jsonl.index``) are NOT matched by the ``*.jsonl``
    glob — ``Path.glob`` treats suffix comparisons strictly — so they're
    excluded automatically.

    Files are read in modification-time order (oldest first) so when
    ``render_sessions`` renders dict iteration order it reflects the
    rough chronological order of sessions.
    """
    from collections import defaultdict

    merged: Dict[str, List[dict]] = defaultdict(list)
    files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                per_file = parse_events(fh)
        except OSError as exc:
            print(
                f"WARNING: skipping {f}: {exc}",
                file=sys.stderr,
            )
            continue
        for session_id, events in per_file.items():
            merged[session_id].extend(events)
    return dict(merged)


if __name__ == "__main__":
    main()
