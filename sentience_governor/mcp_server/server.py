"""Sentience MCP server — v0.3.0 CP1 skeleton.

Rung 2 of the capability ladder: Claude-initiated governance tools. This
module keeps the data-producing logic (the ``*_payload`` functions) FREE of
any ``mcp`` dependency so it is testable without the optional package; the
tool registration in :func:`build_server` is a thin wrapper over those
payloads and over the existing analyzers (no new analyzer logic).

Session-scope discipline (plan §2):
- measured reads operate on the LAST COMPLETED (token-bearing) session;
- ``declare_intent`` (later CP) writes to the CURRENT session;
- the structural-status tool (later CP) is current-session, partial.

CP1 ships the two session-independent reads only: ``sentience_explain`` and
``sentience_profile_view``.

Install: ``pip install "sentience-governor[mcp]"``. The server is meant to
be spawned by Claude Code (opt-in registration), not run by hand.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentience_governor.analyze.methodology import build_methodology
from sentience_governor.analyze.policy_violation_burn_rate import (
    compute_policy_violation_burn_rate,
)
from sentience_governor.analyze.pulse import compute_pulse
from sentience_governor.analyze.undeclared_intent import (
    compute_undeclared_intent_spend,
)
from sentience_governor.profile.loader import GovernanceProfile

SERVER_NAME = "sentience"


# ---------------------------------------------------------------------------
# Tool payloads — pure, no mcp dependency, thin wrappers over the analyzers.
# ---------------------------------------------------------------------------


def explain_payload() -> Dict[str, Any]:
    """IR-5 methodology: how Sentience counts. Session-independent."""
    return build_methodology()


def profile_view_payload() -> Dict[str, Any]:
    """The operator's declared governance posture (authoritative, read-only,
    session-independent).

    Returns the declared profile plus provenance so a consumer reads the
    *declared* posture rather than inferring one (plan §3.3 / §3.4). When no
    profile file exists, returns the defaults with ``from_file: false``.
    """
    profile = GovernanceProfile.from_default_path_or_none()
    from_file = profile is not None
    if profile is None:
        profile = GovernanceProfile.defaults()
    return {
        "profile": profile.to_dict(),
        "from_file": from_file,
        "source_path": (
            str(profile.source_path) if profile.source_path else None
        ),
        "fingerprint": profile.fingerprint(),
        "schema_version": profile.schema_version,
    }


# ---------------------------------------------------------------------------
# Last-completed-session measured reads (plan §2/§5). Token analysis is only
# available after a session ends, so these operate on the most recent
# COMPLETED (token-bearing) session, never the live one, and NAME it.
# ---------------------------------------------------------------------------


_NO_COMPLETED_SESSION: Dict[str, Any] = {
    "status": "no_completed_session",
    "detail": (
        "No completed (token-bearing) session was found. Token analysis is "
        "only available after a session ends (SessionEnd); the live session "
        "carries no token data yet. Run a Claude Code session to completion "
        "and retry."
    ),
}


def _resolve_last_completed_session() -> Optional[Tuple[str, List[dict], str]]:
    """Return ``(session_id, events, end_time_iso)`` of the most recent
    token-bearing session, EXCLUDING the current live one; or None.

    Reuses the CLI's resolution helpers (no new logic). The live session is
    excluded by id (`CLAUDE_CODE_SESSION_ID`) and is also naturally skipped
    because it is not token-bearing until SessionEnd.
    """
    from sentience_governor.cli.ux import (
        _latest_token_bearing_session,
        _load_session,
        _resolve_trace_dir,
    )

    trace_dir = _resolve_trace_dir()
    current = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    found = _latest_token_bearing_session(trace_dir, current)
    if found is None:
        return None
    session_id, _turn_count = found
    path = trace_dir / f"{session_id}.jsonl"
    _, events = _load_session(path)
    end_iso = datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    return session_id, events, end_iso


def _measured_read(
    compute: Callable[[List[dict]], Dict[str, Any]],
) -> Dict[str, Any]:
    """Run a session analyzer over the last completed session and NAME the
    session read (plan §5 invariant), or return the no-completed-session
    status. Never reads the live session's (absent) token data."""
    resolved = _resolve_last_completed_session()
    if resolved is None:
        return dict(_NO_COMPLETED_SESSION)
    session_id, events, end_iso = resolved
    return {
        "session_id": session_id,
        "session_end": end_iso,
        "result": compute(events),
    }


def pulse_payload() -> Dict[str, Any]:
    """Full pulse of the last completed session."""
    return _measured_read(compute_pulse)


def intent_payload() -> Dict[str, Any]:
    """Undeclared-intent spend of the last completed session."""
    return _measured_read(compute_undeclared_intent_spend)


def violations_payload() -> Dict[str, Any]:
    """Policy-violation burn rate of the last completed session."""
    return _measured_read(compute_policy_violation_burn_rate)


# ---------------------------------------------------------------------------
# Current/live-session structural status (plan §5). STRUCTURAL COUNTS ONLY:
# never a token / burn / economics / pulse / inferred field (contract
# invariant "no live token claims"). Token analysis is unavailable until the
# session ends, and that is surfaced explicitly.
# ---------------------------------------------------------------------------


def session_status_payload(
    *,
    env: Optional[Dict[str, str]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Structural status of the CURRENT/live session: event count, tool-call
    counts (by operation class), and policy-violation / advisory-flag counts
    so far. Partial by nature; carries an explicit
    ``token_analysis: "unavailable until SessionEnd"``. Fail-closed via the §7
    resolver: if no live session can be identified, returns that status and no
    counts (``env`` / ``now`` are injectable for testing)."""
    from sentience_governor.cli.ux import (
        _load_session,
        _resolve_trace_dir,
        _split_anomaly_counts,
        classify_session,
    )
    from sentience_governor.mcp_server.session_identity import (
        RESOLVED,
        resolve_current_session,
    )

    trace_dir = _resolve_trace_dir()
    # Read path: the loose freshness window (UX tolerance). A wrong or absent
    # identification here is a read-only, clearly-partial answer that persists
    # nothing, so tolerating an idle-but-live trace is preferable to failing
    # closed on every reading gap. (declare_intent uses the tight write gate.)
    resolution = resolve_current_session(trace_dir, env=env, now=now)
    base: Dict[str, Any] = {
        "token_analysis": "unavailable until SessionEnd",
        "partial": True,
        "resolution": resolution.reason,
    }
    if resolution.status != RESOLVED or resolution.session_id is None:
        base["status"] = resolution.status
        return base

    session_id = resolution.session_id
    _, events = _load_session(trace_dir / f"{session_id}.jsonl")
    policy_violations, advisory_flags = _split_anomaly_counts(
        classify_session(events)
    )
    # Extract ONLY the structural tool-call block; the analyzer's token
    # attribution is deliberately dropped (contract: no live token claims).
    tool_calls = compute_undeclared_intent_spend(events).get("tool_calls", {})
    base.update(
        status="current_session",
        session_id=session_id,
        event_count=len(events),
        tool_calls=tool_calls,
        policy_violations_so_far=policy_violations,
        advisory_flags_so_far=advisory_flags,
    )
    return base


# ---------------------------------------------------------------------------
# declare_intent — the one forward-looking write (plan §6). Server-written
# provenance, append-only, fail-closed on uncertain binding. The agent
# supplies only the objective + scope; everything else is server-controlled.
# ---------------------------------------------------------------------------


def declare_intent_payload(
    objective: str,
    scope: List[str],
    *,
    env: Optional[Dict[str, str]] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Append an agent-declared ``INTENT_DECLARED`` to the CURRENT session.

    Maps to ``stated_objective`` + ``session_scope_hint`` with an
    agent-declared ``intent_source`` (never ``none``). Requires a non-empty
    objective AND a non-empty scope: an empty scope still trips
    ``SCOPE_INTENT_MISMATCH`` (plan §6), so scope is load-bearing. Fails
    closed via the §7 resolver — on any uncertain binding it writes nothing.
    ``env`` / ``now`` are injectable for testing."""
    if not isinstance(objective, str) or not objective.strip():
        return {
            "status": "invalid_request",
            "written": False,
            "detail": "objective must be a non-empty string",
        }
    if (
        not isinstance(scope, list)
        or not scope
        or not all(isinstance(s, str) and s.strip() for s in scope)
    ):
        return {
            "status": "invalid_request",
            "written": False,
            "detail": (
                "scope must be a non-empty list of non-empty strings (the "
                "operation targets this declaration authorizes); an empty "
                "scope still trips a mismatch, so it is required"
            ),
        }
    clean_objective = objective.strip()
    clean_scope = [s.strip() for s in scope]

    from sentience_governor.cli.ux import _resolve_trace_dir
    from sentience_governor.mcp_server.session_identity import (
        RESOLVED,
        WRITE_FRESHNESS_WINDOW_SECONDS,
        resolve_current_session,
    )
    from sentience_governor.wrapper.claude_code_hook import (
        ClaudeCodeGovernanceHook,
    )

    trace_dir = _resolve_trace_dir()
    # Write path: use the TIGHT freshness window (safety gate). A declaration
    # stamps provenance, so a stale spawn-time env whose prior-session trace is
    # more than WRITE_FRESHNESS_WINDOW_SECONDS old must fail closed rather than
    # risk misattributing the declaration to that prior session.
    resolution = resolve_current_session(
        trace_dir,
        env=env,
        now=now,
        freshness_window=WRITE_FRESHNESS_WINDOW_SECONDS,
    )
    if resolution.status != RESOLVED or resolution.session_id is None:
        # Fail closed: a misattributed declaration is worse than none.
        return {
            "status": resolution.status,
            "written": False,
            "detail": resolution.reason,
        }

    session_id = resolution.session_id
    sink_path = trace_dir / f"{session_id}.jsonl"
    hook = ClaudeCodeGovernanceHook({}, sink_path=sink_path)
    event_id = hook.emit_intent_declaration(
        session_id, clean_objective, clean_scope
    )
    if event_id is None:
        return {
            "status": "not_written",
            "written": False,
            "detail": (
                "the session trace could not be appended to (no prior trace "
                "to chain onto); nothing was written"
            ),
        }
    return {
        "status": "declared",
        "written": True,
        "session_id": session_id,
        "event_id": event_id,
        "stated_objective": clean_objective,
        "session_scope_hint": clean_scope,
        # Agent runtime self-declaration: extractor-reliable but
        # content-untrusted, NOT integrator-vouched (intent-declaration-
        # honesty.md) — so inferred / inferred_low, never explicit.
        "intent_source": "inferred",
        "intent_confidence": "inferred_low",
        "binding": resolution.source,
    }


# ---------------------------------------------------------------------------
# MCP server — thin registration over the payloads (requires optional `mcp`).
# ---------------------------------------------------------------------------


def build_server() -> Any:
    """Construct the FastMCP server with the CP1 tool set.

    Requires the optional ``mcp`` dependency; raises SystemExit with an
    install hint if it is missing.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised via install hint
        raise SystemExit(
            "The Sentience MCP server requires the optional 'mcp' dependency. "
            'Install it with: pip install "sentience-governor[mcp]"'
        ) from exc

    server = FastMCP(SERVER_NAME)

    @server.tool()
    def sentience_explain() -> Dict[str, Any]:
        """Explain how Sentience counts: the token classes, the dedupe rule,
        the per-turn (not per-tool) attribution boundary, the operation-type
        enum, and the join-key semantics. Methodology only; no session data.
        """
        return explain_payload()

    @server.tool()
    def sentience_profile_view() -> Dict[str, Any]:
        """Return the operator's declared governance profile (posture).
        Read-only and session-independent. This is the declared posture, not
        a Claude-inferred one.
        """
        return profile_view_payload()

    @server.tool()
    def sentience_pulse() -> Dict[str, Any]:
        """Full pulse (undeclared-intent spend, policy-violation burn rate,
        advisory flags, tool calls, tool-token attribution) of the LAST
        COMPLETED session. Token analysis needs a finished session, so this
        reads the most recent completed one, NOT the live session; the return
        names the session read. Returns a `no_completed_session` status when
        none exists.
        """
        return pulse_payload()

    @server.tool()
    def sentience_intent() -> Dict[str, Any]:
        """Undeclared-intent spend of the LAST COMPLETED session (not the live
        session). The return names the session read. Returns a
        `no_completed_session` status when none exists.
        """
        return intent_payload()

    @server.tool()
    def sentience_violations() -> Dict[str, Any]:
        """Policy-violation burn rate of the LAST COMPLETED session (not the
        live session). The return names the session read. Returns a
        `no_completed_session` status when none exists.
        """
        return violations_payload()

    @server.tool()
    def sentience_session_status() -> Dict[str, Any]:
        """Structural status of the CURRENT/live session: event count,
        tool-call counts by operation class, and policy-violation / advisory
        counts so far. STRUCTURAL ONLY: no token, burn, economics, pulse, or
        inferred figure (token analysis is unavailable until SessionEnd, and
        the return says so). Partial by nature. Fails closed to a status with
        no counts if the live session cannot be identified.
        """
        return session_status_payload()

    @server.tool()
    def sentience_declare_intent(
        objective: str, scope: List[str]
    ) -> Dict[str, Any]:
        """Declare, for THIS session, the objective you are working toward and
        the operation targets it authorizes (its scope). Sentience records it
        as a server-written INTENT_DECLARED event on the current session's
        trace, append-only. This is forward-looking: it affects only
        SUBSEQUENT activity, never retroactively cleans earlier events. It
        writes nothing if the live session cannot be identified with
        confidence. `scope` is required and must cover the targets you will
        act on (e.g. ["filesystem"], ["shell"], ["web"]); an empty or
        mismatched scope still trips a scope-intent mismatch.
        """
        return declare_intent_payload(objective, scope)

    return server


def main() -> None:
    """Console entry point (`sentience-mcp-server`): run the stdio server."""
    build_server().run()  # stdio transport by default


if __name__ == "__main__":
    main()
