"""Claude Code hook adapter — governance events for coding-agent sessions.

Runtime shape
-------------
Claude Code's hook system spawns a fresh subprocess on every tool
invocation, handing it a JSON payload on stdin. This module is the
CLI entry that subprocess runs. One invocation, one process, one or
more governance events appended to a shared sink file.

Because every invocation is a new process, ``SessionManager`` state
cannot persist in memory across hook calls. Chain continuity
(``event_sequence_number``, ``previous_event_id``) is reconstructed
from the sink on disk via
``sentience_governor.session_manager.resumption``. The sidecar index
keeps reconstruction O(1) at the cost of a single atomic file write
per invocation.

Hook payload contract (from Claude Code docs)
---------------------------------------------
Two relevant hook events, both delivered as JSON on stdin:

``PreToolUse``::

    {
      "hook_event_name": "PreToolUse",
      "session_id": "<claude-code-session-id>",
      "tool_name": "Bash" | "Edit" | "Write" | "Read" | "Grep" |
                   "Glob" | "WebFetch" | "WebSearch" | "Agent" |
                   "NotebookEdit" | "mcp__<server>__<tool>",
      "tool_input": { ... tool-specific ... },
      "tool_use_id": "<id>",
      "cwd": "<path>"
    }

``PostToolUse``::

    {
      "hook_event_name": "PostToolUse",
      "session_id": "<claude-code-session-id>",
      "tool_name": "<same as Pre>",
      "tool_input": { ... },
      "tool_response": { ... } | "<string>",
      "tool_use_id": "<id>",
      "cwd": "<path>"
    }

Unknown hook events are ignored cleanly (the hook exits 0 with no
governance emission). Additional fields in either payload are
tolerated — the hook never fails on unexpected keys.

Event emission (per plan §4)
----------------------------
First invocation for a new ``session_id``::

    AGENT_REGISTERED  → INTENT_DECLARED (intent_source=none)
      → SCOPE_ASSERTED  → CONTEXT_SNAPSHOT (pre-call)
      [→ MEMORY_WRITE_ATTEMPT if persistence target]

Subsequent ``PreToolUse``::

    SCOPE_ASSERTED  → CONTEXT_SNAPSHOT (pre-call, using tool_input)
      [→ MEMORY_WRITE_ATTEMPT if persistence target]

Every ``PostToolUse``::

    CONTEXT_SNAPSHOT (post-call, using tool_response)

Intent stays ``intent_source=none`` / ``intent_confidence=unknown``
throughout. Claude Code does not expose the user's prompt to hooks;
fabricating intent would be dishonest. ``INTENT_MISSING`` fires.
Matches the failure-first walkthrough in userdocs §3.1.

Fail-open discipline
--------------------
Every error path in this module returns exit code 0. Claude Code
treats non-zero exit from a hook as a reason to abort tool
execution; we must never do that. Malformed JSON on stdin, sink
write failures, missing directories, lock contention on
unsupported platforms — all log to stderr and exit 0.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.event_builder.builder import EventBuilder
from sentience_governor.profile import GovernanceProfile
from sentience_governor.schema.events import (
    ClassificationSource,
    DeploymentMode,
    DetectionMechanism,
    IntentConfidence,
    IntentSource,
    OperationType,
    WriteType,
)
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.wrapper.token_extraction import (
    CANONICAL_TOKEN_FIELDS,
    PROVIDER_ANTHROPIC,
    derive_turn_token_burn,
    extract_anthropic_usage,
)
from sentience_governor.wrapper.claude_code_transcript import parse_transcript_file
from sentience_governor.session_manager.resumption import (
    ResumedState,
    file_size,
    read_emitted_turns,
    record_emitted_turns,
    resume_session_state,
    sink_lock,
    update_session_state,
)
from sentience_governor.sink.writer import FileSink, SinkWriter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults / environment configuration
# ---------------------------------------------------------------------------

# Per-session default: one file per session_id under a directory. Keeps
# free-tier operators from accumulating a single unbounded JSONL over
# weeks of use. Each session's file caps at whatever that session
# produces (typically a few MB); cleanup is a plain `find ... -mtime +N
# -delete`. See userdocs §7.
_DEFAULT_SINK_DIR = Path.home() / ".sentience" / "traces" / "claude-code"
_FALLBACK_SINK_DIR = Path("/tmp/sentience-claude-code-traces")

# Back-compat: if the user's SENTIENCE_CLAUDE_CODE_SINK_PATH (or the
# pre-v0.1.9 default) points at a path ending in .jsonl, the hook
# writes to that single file — the shared-file mode shipped earlier.
# Operators who set that explicitly get exactly what they asked for.
_SHARED_FILE_SUFFIX = ".jsonl"

_DEFAULT_AGENT_ID_PREFIX = "claude-code"
_DEFAULT_DEPLOYMENT_MODE = DeploymentMode.vendor_managed
_DEFAULT_SIZE_WARN_MB = 50
_VENDOR_ID = "claude-code"
_AGENT_VERSION = "0.1.9"  # stamp sentience_governor runtime version here
_DECLARED_CAPABILITIES = ["coding_agent.observe"]


# Claude Code built-in tool → (OperationType, target_system)
_BUILTIN_TOOL_MAP: Dict[str, Tuple[OperationType, str]] = {
    "Bash": (OperationType.EXECUTE, "shell"),
    "Edit": (OperationType.WRITE, "filesystem"),
    "Write": (OperationType.WRITE, "filesystem"),
    "NotebookEdit": (OperationType.WRITE, "filesystem"),
    "Read": (OperationType.READ, "filesystem"),
    "Grep": (OperationType.READ, "filesystem"),
    "Glob": (OperationType.READ, "filesystem"),
    "WebFetch": (OperationType.READ, "web"),
    "WebSearch": (OperationType.READ, "web"),
    "Agent": (OperationType.EXECUTE, "agent_runtime"),
    "Task": (OperationType.EXECUTE, "agent_runtime"),
    "TodoWrite": (OperationType.WRITE, "session_state"),
}

# Persistence keywords that fire MEMORY_WRITE_ATTEMPT. Matches the MCP
# wrapper's _PERSISTENCE_KEYWORDS for consistency, plus "filesystem"
# so Edit / Write / NotebookEdit fire.
_PERSISTENCE_KEYWORDS = {
    "database",
    "vector_store",
    "filesystem",
    "logging",
    "storage",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_sink_base() -> Tuple[Path, bool]:
    """Return ``(base_path, shared_file_mode)`` from env or defaults.

    Does NOT know the ``session_id`` yet — that is not available until
    the hook payload is parsed. The caller (``ClaudeCodeGovernanceHook``)
    resolves the per-invocation sink file from this base via
    :func:`_session_file_for` after ``_build_context``.

    Two modes, disambiguated by the shape of the configured path:

    * **Per-session directory mode** (new default, free-tier friendly).
      The configured path is either unset or a directory (no ``.jsonl``
      suffix). Each session's trace lives in its own file under the
      directory, bounded by that session's own activity.

    * **Shared-file mode** (back-compat, explicit opt-in).
      The configured path ends in ``.jsonl``. The hook writes every
      session's events to that single file, interleaved by
      ``session_id``. Matches the pre-v0.1.9 behaviour.
    """
    raw = os.environ.get("SENTIENCE_CLAUDE_CODE_SINK_PATH", "").strip()
    if raw:
        base = Path(raw).expanduser()
        shared_file_mode = base.suffix == _SHARED_FILE_SUFFIX
    else:
        base = _DEFAULT_SINK_DIR
        shared_file_mode = False
    return base, shared_file_mode


def _session_file_for(
    base: Path, shared_file_mode: bool, session_id: str
) -> Path:
    """Return the actual sink file for ``session_id`` and create parent dirs.

    On a read-only home directory (rare: some container or CI
    environments), falls back to a per-session file under /tmp with a
    stderr warning.
    """
    if shared_file_mode:
        candidate = base
        parent_dir = candidate.parent
    else:
        stem = session_id or "unknown"
        candidate = base / f"{stem}.jsonl"
        parent_dir = base

    try:
        parent_dir.mkdir(parents=True, exist_ok=True)
        return candidate
    except OSError as exc:
        fallback_dir = _FALLBACK_SINK_DIR
        fallback_file = fallback_dir / f"{session_id or 'unknown'}.jsonl"
        print(
            f"sentience: cannot create {parent_dir}: {exc}; "
            f"falling back to {fallback_file}",
            file=sys.stderr,
        )
        try:
            fallback_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return fallback_file


def _maybe_warn_sink_size(sink_path: Path) -> None:
    """Print a one-line warning if the sink has grown past the threshold."""
    raw = os.environ.get("SENTIENCE_CLAUDE_CODE_SIZE_WARN_MB", "").strip()
    try:
        threshold_mb = int(raw) if raw else _DEFAULT_SIZE_WARN_MB
    except ValueError:
        threshold_mb = _DEFAULT_SIZE_WARN_MB
    if threshold_mb <= 0:
        return
    try:
        size_bytes = sink_path.stat().st_size
    except OSError:
        return
    mb = size_bytes / (1024 * 1024)
    if mb >= threshold_mb:
        print(
            f"sentience: trace file exceeds {threshold_mb}MB "
            f"(current: {mb:.0f}MB) — consider rotating via "
            f"SENTIENCE_CLAUDE_CODE_SINK_PATH",
            file=sys.stderr,
        )


def _estimate_tokens(data: Any) -> int:
    try:
        return max(1, len(json.dumps(data, default=str)) // 4)
    except Exception:
        return 1


def _derive_agent_id(session_id: str) -> str:
    prefix = (
        os.environ.get("SENTIENCE_CLAUDE_CODE_AGENT_ID_PREFIX", "").strip()
        or _DEFAULT_AGENT_ID_PREFIX
    )
    short = (session_id or "unknown")[:8]
    return f"{prefix}-{short}"


def _resolve_deployment_mode() -> DeploymentMode:
    raw = os.environ.get("SENTIENCE_CLAUDE_CODE_DEPLOYMENT_MODE", "").strip()
    if not raw:
        return _DEFAULT_DEPLOYMENT_MODE
    try:
        return DeploymentMode(raw)
    except ValueError:
        logger.warning(
            "unknown deployment mode %r; using default %s",
            raw,
            _DEFAULT_DEPLOYMENT_MODE.value,
        )
        return _DEFAULT_DEPLOYMENT_MODE


def _infer_tool_mapping(tool_name: str) -> Tuple[OperationType, str]:
    """Map a Claude Code tool name to (OperationType, target_system).

    * Built-in tools use the explicit table.
    * ``mcp__<server>__<tool>`` names split on ``__``: server becomes
      target_system, and the trailing tool name is keyword-matched to
      an OperationType (same heuristic the MCP wrapper uses).
    * Unknown tool names default to READ / ``<tool_name>``.
    """
    if tool_name in _BUILTIN_TOOL_MAP:
        return _BUILTIN_TOOL_MAP[tool_name]
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__", 2)
        if len(parts) >= 3:
            _, server, tool_part = parts
            return _infer_op_from_name(tool_part), server
        if len(parts) == 2:
            return OperationType.READ, parts[1]
    return OperationType.READ, tool_name


def _infer_op_from_name(name: str) -> OperationType:
    lower = name.lower()
    if any(k in lower for k in ("write", "update", "create", "insert", "put")):
        return OperationType.WRITE
    if any(k in lower for k in ("delete", "remove", "drop")):
        return OperationType.DELETE
    if any(k in lower for k in ("exec", "run", "execute")):
        return OperationType.EXECUTE
    return OperationType.READ


def _is_persistence_target(
    target_system: str,
    tool_name: str,
    operation_type: Optional[OperationType] = None,
) -> bool:
    """True iff the tool call should fire MEMORY_WRITE_ATTEMPT.

    Two conditions must both hold:

    * The target_system or tool_name matches a persistence keyword.
    * The operation is WRITE or DELETE (not READ, not EXECUTE).

    Excluding READ here is critical: filesystem ``Read`` / ``Grep`` /
    ``Glob`` all target the filesystem but are not writes and must not
    fire a memory-write event. Excluding EXECUTE documents the Bash
    blind spot: shell commands can persist but we do not attempt to
    parse them in v0.
    """
    blob = f"{target_system} {tool_name}".lower()
    keyword_match = any(kw in blob for kw in _PERSISTENCE_KEYWORDS)
    if not keyword_match:
        return False
    if operation_type is None:
        return True  # legacy call paths retain prior behaviour
    return operation_type in (OperationType.WRITE, OperationType.DELETE)


# ---------------------------------------------------------------------------
# v0.3.0 CP5 — intent-baseline rehydration
# ---------------------------------------------------------------------------


def _first_declared_intent(
    sink_path: Path,
) -> Optional[Tuple[Optional[str], List[str]]]:
    """Return the ``(stated_objective, session_scope_hint)`` of the FIRST real
    intent declaration in a session trace, or None.

    "Real" means an ``INTENT_DECLARED`` event whose ``intent_source`` is not
    ``none``. The auto-emitted session-start ``INTENT_DECLARED`` carries
    ``intent_source=none`` (the "no intent" marker) and is deliberately
    skipped: it is not a declaration, and letting it win the cache's
    first-write-wins would mask a later real declaration (e.g. one appended by
    ``declare_intent``). Returning the first real declaration preserves
    first-write-wins among genuine declarations.

    A cheap substring gate avoids JSON-parsing every line, so a
    declaration-free session pays only a scan, never a parse — and, finding
    nothing, sets no baseline (keeping such sessions byte-identical).
    """
    try:
        with sink_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if "INTENT_DECLARED" not in line:
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if event.get("event_type") != "INTENT_DECLARED":
                    continue
                payload = event.get("payload") or {}
                if payload.get("intent_source", "none") == "none":
                    continue
                scope = payload.get("session_scope_hint") or []
                if not isinstance(scope, list):
                    scope = []
                return payload.get("stated_objective"), scope
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Core hook processor
# ---------------------------------------------------------------------------


@dataclass
class _HookContext:
    hook_event_name: str
    session_id: str
    tool_name: str
    tool_input: dict
    tool_response: Any
    agent_id: str
    deployment_mode: DeploymentMode
    # v0.2.6.1 — the join key Claude Code assigns each tool call. Present on
    # Pre/PostToolUse (identical across the pair); absent on SessionEnd.
    tool_use_id: Optional[str] = None
    # v0.2.6.1 — path to the flushed transcript JSONL. Present on
    # Pre/Post/SessionEnd; SessionEnd is where we parse it for per-turn tokens.
    transcript_path: Optional[str] = None
    # v0.2.3 Track 2 — LLM token usage extracted from the hook payload
    # if available. Anthropic's Claude Code hook does NOT currently
    # surface token usage; these fields stay all-None until/unless
    # they do. The adapter below probes defensively so when Anthropic
    # adds the field, we pick it up automatically with no further
    # changes to this module.
    llm_usage: Dict[str, Optional[int]] = None  # type: ignore[assignment]


class ClaudeCodeGovernanceHook:
    """Handles one Claude Code hook invocation end to end.

    A new instance is constructed per subprocess. ``process()`` is
    idempotent from the caller's perspective: it either appends the
    appropriate events and returns, or logs a warning and returns
    (fail-open). It never raises.
    """

    def __init__(
        self,
        hook_payload: dict,
        sink_path: Optional[Path] = None,
        *,
        sink_base: Optional[Path] = None,
        shared_file_mode: Optional[bool] = None,
    ) -> None:
        """Construct a per-invocation hook processor.

        Two construction styles, kept separate for clarity:

        * ``sink_path=<file>`` (legacy / tests) — pin the hook to a
          specific file regardless of session_id. Maintains
          back-compat with tests written before per-session mode and
          is how the shared-file path looks end-to-end.
        * ``sink_base=<dir>, shared_file_mode=False`` (new default) —
          hook resolves ``<dir>/<session_id>.jsonl`` after parsing
          the payload. Used by ``run_hook`` when the operator has not
          pointed the env var at a ``.jsonl`` file.

        If both are supplied, ``sink_path`` wins (explicit override).
        """
        self._payload = hook_payload if isinstance(hook_payload, dict) else {}
        self._sink_path_override = sink_path
        self._sink_base = sink_base
        self._shared_file_mode = shared_file_mode

    def process(self) -> None:
        """Entry point. All exceptions caught and logged."""
        try:
            self._process_inner()
        except Exception as exc:  # pragma: no cover - belt-and-suspenders
            logger.warning("claude_code_hook: unhandled error: %s", exc)

    def emit_intent_declaration(
        self,
        session_id: str,
        stated_objective: str,
        session_scope_hint: List[str],
        *,
        intent_source: IntentSource = IntentSource.inferred,
        intent_confidence: IntentConfidence = IntentConfidence.inferred_low,
    ) -> Optional[str]:
        """Append a server-written ``INTENT_DECLARED`` to an EXISTING session
        trace, using the same locked append ceremony as tool-event capture
        (v0.3.0 CP6 — the ``declare_intent`` write path).

        The agent supplies only ``stated_objective`` + ``session_scope_hint``;
        every other field is server-controlled. Append-only: this never edits
        prior events. Returns the new event's id, or None if nothing was
        written (e.g. the session has no prior trace — the caller must have
        confirmed capture and identity first, §7).

        ``intent_source``/``intent_confidence`` default to
        ``inferred``/``inferred_low``: an agent's runtime self-declaration is
        extractor-reliable but content-untrusted, NOT integrator-vouched — so
        it must not be labelled ``explicit`` (intent-declaration-honesty.md).
        """
        sink_path = self._resolve_sink_path_for(session_id)
        agent_id = _derive_agent_id(session_id)
        deployment_mode = _resolve_deployment_mode()
        with sink_lock(sink_path):
            resumed = resume_session_state(sink_path, session_id)
            if resumed is None:
                # No prior trace to chain onto. v1 never creates a trace from
                # the declaration path (§7); fail closed to the caller.
                return None

            session_manager = SessionManager()
            cache = InProcessCache()
            sink = SinkWriter(FileSink(str(sink_path)))
            profile = GovernanceProfile.from_default_path_or_none()
            session_manager.session_start(
                session_id=session_id,
                agent_id=agent_id,
                initial_sequence=resumed.last_sequence,
                initial_last_event_id=resumed.last_event_id,
                profile=profile,
            )
            cache.init_session(session_id)
            builder = EventBuilder(
                session_manager=session_manager,
                cache=cache,
                agent_id=agent_id,
                session_id=session_id,
                deployment_mode=deployment_mode,
            )
            event = builder.build_intent_declared(
                stated_objective=stated_objective,
                intent_source=intent_source,
                intent_confidence=intent_confidence,
                authorization_claim=None,
                session_scope_hint=list(session_scope_hint),
            )
            if event is None:
                return None
            sink.write(event, session_id)
            self._refresh_sidecar(session_manager, session_id, sink_path)
            return event.event_id

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    def _process_inner(self) -> None:
        ctx = self._build_context()
        if ctx is None:
            return

        sink_path = self._resolve_sink_path_for(ctx.session_id)

        _maybe_warn_sink_size(sink_path)

        # Lock covers the entire read-plus-append-plus-sidecar-update
        # critical section. Never read or write the sink outside this
        # block when resumption is in play.
        with sink_lock(sink_path):
            resumed = resume_session_state(sink_path, ctx.session_id)

            # Compose the per-session primitives. These are ephemeral;
            # they live only for the lifetime of this invocation.
            session_manager = SessionManager()
            cache = InProcessCache()
            sink = SinkWriter(FileSink(str(sink_path)))

            initial_sequence = resumed.last_sequence if resumed else 0
            initial_last_event_id = resumed.last_event_id if resumed else None

            # v0.2.5: load operator-authored governance profile if
            # present. None when no file exists — keeps the v0.2.4
            # code path for resumption-based hook sessions.
            profile = GovernanceProfile.from_default_path_or_none()
            session_manager.session_start(
                session_id=ctx.session_id,
                agent_id=ctx.agent_id,
                initial_sequence=initial_sequence,
                initial_last_event_id=initial_last_event_id,
                profile=profile,
            )
            cache.init_session(ctx.session_id)

            # v0.3.0 CP5 — rehydrate a prior intent declaration (if any) from
            # this session's own trace BEFORE the current event is evaluated,
            # so a declaration appended in an earlier hook process (each is a
            # fresh cache) is visible to POL-001 evaluation now. Only real
            # declarations (intent_source != none) count; the cache preserves
            # first-write-wins. This never edits prior events, and a
            # declaration-free session sets no baseline (byte-identical).
            # Skipped on the first event (nothing prior to rehydrate).
            if resumed is not None:
                declared_intent = _first_declared_intent(sink_path)
                if declared_intent is not None:
                    stated_objective, scope_hint = declared_intent
                    cache.set_intent_baseline(
                        ctx.session_id,
                        stated_objective=stated_objective,
                        scope_hint=scope_hint,
                    )

            builder = EventBuilder(
                session_manager=session_manager,
                cache=cache,
                agent_id=ctx.agent_id,
                session_id=ctx.session_id,
                deployment_mode=ctx.deployment_mode,
            )

            is_first_event = resumed is None

            if is_first_event:
                self._emit_agent_registered(builder, sink, ctx)
                self._emit_intent_declared(builder, sink, ctx)

            if ctx.hook_event_name == "PreToolUse":
                self._emit_pre_tool(builder, sink, ctx)
            elif ctx.hook_event_name == "PostToolUse":
                self._emit_post_tool(builder, sink, ctx)
            elif ctx.hook_event_name == "SessionEnd":
                # v0.2.6.1 — parse the flushed transcript and append one
                # token-bearing CONTEXT_SNAPSHOT per model turn (requestId).
                # Fail-open + idempotent (see the helper).
                self._emit_session_end_token_batch(builder, sink, ctx, sink_path)
            else:
                # SessionStart / UserPromptSubmit etc. For a new session we've
                # already emitted REG + INTENT above, which is the correct
                # "session started" artifact. Other hook events are no-ops.
                if not is_first_event:
                    logger.debug(
                        "claude_code_hook: ignoring hook_event_name=%r "
                        "with no registration",
                        ctx.hook_event_name,
                    )

            # Update sidecar with the post-append state. We query the
            # sink size AFTER the writes so the recorded file_offset
            # points at the last line we appended.
            self._refresh_sidecar(session_manager, ctx.session_id, sink_path)

    # ------------------------------------------------------------------
    # Context extraction
    # ------------------------------------------------------------------

    def _build_context(self) -> Optional[_HookContext]:
        """Extract the fields we care about; reject malformed payloads.

        Returns None (caller exits 0) if the payload lacks the minimum
        fields to emit any event.
        """
        hook_event_name = self._payload.get("hook_event_name")
        session_id = self._payload.get("session_id")
        tool_name = self._payload.get("tool_name") or ""
        if not isinstance(hook_event_name, str) or not hook_event_name:
            logger.debug("claude_code_hook: missing hook_event_name; skipping")
            return None
        if not isinstance(session_id, str) or not session_id:
            logger.debug("claude_code_hook: missing session_id; skipping")
            return None

        tool_input = self._payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}

        # tool_response is free-form (dict/str/None/list/number). Pass through.
        tool_response = self._payload.get("tool_response")

        # v0.2.6.1 join key + transcript path. Both best-effort: absent on
        # older Claude Code, present on current. Non-string → None.
        tool_use_id = self._payload.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            tool_use_id = None
        transcript_path = self._payload.get("transcript_path")
        if not isinstance(transcript_path, str) or not transcript_path:
            transcript_path = None

        # v0.2.3 Track 2 — defensive token extraction from the hook
        # payload. Today Anthropic's Claude Code hook payload does NOT
        # carry token usage, so this typically returns all-None. The
        # adapter is wired so that IF Anthropic adds the field
        # upstream, we pick it up automatically. We probe both
        # ``usage`` and ``token_usage`` (likely future key names) and
        # the extractor itself is exception-safe.
        llm_usage = self._extract_token_usage_from_payload()

        return _HookContext(
            hook_event_name=hook_event_name,
            session_id=session_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_response=tool_response,
            llm_usage=llm_usage,
            agent_id=_derive_agent_id(session_id),
            deployment_mode=_resolve_deployment_mode(),
            tool_use_id=tool_use_id,
            transcript_path=transcript_path,
        )

    def _extract_token_usage_from_payload(self) -> Dict[str, Optional[int]]:
        """v0.2.3 Track 2 — best-effort token extraction from hook payload.

        Probes the payload for ``usage`` or ``token_usage`` keys (the
        likely shapes Anthropic might use if/when they surface token
        data in the Claude Code hook). Returns a canonical-fields dict
        with all-None values when no usage data is present, which is
        the default state today.

        Defensive: never raises. Any extraction failure returns
        all-None, leaving the agent's primary path unaffected.
        """
        for key in ("usage", "token_usage"):
            value = self._payload.get(key)
            if value:
                try:
                    return extract_anthropic_usage(value)
                except Exception:
                    logger.debug(
                        "claude_code_hook: extract_anthropic_usage raised on key %r; "
                        "falling back to all-None",
                        key,
                        exc_info=True,
                    )
                    break
        return {field: None for field in CANONICAL_TOKEN_FIELDS}

    # ------------------------------------------------------------------
    # Event emission helpers
    # ------------------------------------------------------------------

    def _emit_agent_registered(
        self, builder: EventBuilder, sink: SinkWriter, ctx: _HookContext
    ) -> None:
        event = builder.build_agent_registered(
            agent_version=_AGENT_VERSION,
            vendor_id=_VENDOR_ID,
            declared_capabilities=_DECLARED_CAPABILITIES,
            owner_claim=os.environ.get("USER") or None,
        )
        if event:
            sink.write(event, ctx.session_id)

    def _emit_intent_declared(
        self, builder: EventBuilder, sink: SinkWriter, ctx: _HookContext
    ) -> None:
        event = builder.build_intent_declared(
            stated_objective=None,
            intent_source=IntentSource.none,
            intent_confidence=IntentConfidence.unknown,
            authorization_claim=None,
            session_scope_hint=_DECLARED_CAPABILITIES,
        )
        if event:
            sink.write(event, ctx.session_id)

    def _emit_pre_tool(
        self, builder: EventBuilder, sink: SinkWriter, ctx: _HookContext
    ) -> None:
        op_type, target_system = _infer_tool_mapping(ctx.tool_name)

        scope_event = builder.build_scope_asserted(
            tool_id=ctx.tool_name,
            asserted_permissions=[op_type.value.lower()],
            target_system=target_system,
            operation_type=op_type,
            authorization_claim=None,
            tool_use_id=ctx.tool_use_id,
        )
        if scope_event:
            sink.write(scope_event, ctx.session_id)

        # Pre-call CONTEXT_SNAPSHOT reflects the tool input about to be
        # sent — the current data under consideration. The MCP wrapper
        # emits CONTEXT_SNAPSHOT post-call; here we have separate Pre
        # and Post hooks, so we emit twice (once pre, once post) to
        # capture both ends.
        ctx_event = builder.build_context_snapshot(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=[target_system],
            retention_flags=[],
            context_size_tokens=_estimate_tokens(ctx.tool_input),
            authorization_claim=None,
            # v0.2.6.1 — carry the join key so a POL-003/POL-005 violation on
            # this pre-call snapshot can be attributed to its model turn.
            tool_use_id=ctx.tool_use_id,
            # v0.2.3 Track 2 — token usage extracted from hook payload
            # if present (typically all-None until Anthropic surfaces
            # it). The serializer drops None fields automatically so
            # non-token-aware payloads see no schema bloat.
            **(ctx.llm_usage or {}),
        )
        if ctx_event:
            sink.write(ctx_event, ctx.session_id)

        if _is_persistence_target(target_system, ctx.tool_name, op_type):
            mem_event = builder.build_memory_write_attempt(
                write_type=WriteType.write_to_persistence_target,
                detection_mechanism=DetectionMechanism.tool_metadata,
                target_store=target_system,
                write_classification="unclassified",
                write_size_tokens=_estimate_tokens(ctx.tool_input),
                retention_requested=None,
                # v0.2.6.1 — join key so POL-004 attributes to its model turn.
                tool_use_id=ctx.tool_use_id,
            )
            if mem_event:
                sink.write(mem_event, ctx.session_id)

    def _emit_post_tool(
        self, builder: EventBuilder, sink: SinkWriter, ctx: _HookContext
    ) -> None:
        _op_type, target_system = _infer_tool_mapping(ctx.tool_name)
        ctx_event = builder.build_context_snapshot(
            data_classifications=[],
            classification_source=ClassificationSource.unclassified,
            provenance=[target_system],
            retention_flags=[],
            context_size_tokens=_estimate_tokens(ctx.tool_response),
            authorization_claim=None,
            # v0.2.6.1 — same join key as the pre-call snapshot (identical
            # tool_use_id across the Pre/Post pair).
            tool_use_id=ctx.tool_use_id,
            # v0.2.3 Track 2 — same token usage attached to the post-call
            # snapshot too, so pre/post events from the same tool call
            # share the same usage attribution.
            **(ctx.llm_usage or {}),
        )
        if ctx_event:
            sink.write(ctx_event, ctx.session_id)

    def _emit_session_end_token_batch(
        self,
        builder: EventBuilder,
        sink: SinkWriter,
        ctx: _HookContext,
        sink_path: Path,
    ) -> None:
        """Parse the SessionEnd transcript → append per-turn token snapshots.

        One token-bearing CONTEXT_SNAPSHOT per transcript ``requestId``
        (llm_turn_id = requestId), carrying the turn's canonical token fields,
        provider/model, and the ``tool_use_ids`` that turn issued (the reverse
        map the analyzer joins live violations against). D7: emit for ALL valid
        turns — including no-tool-call turns — so total session burn is
        complete and distinguishable from governance-attributable burn.

        Fail-open: a missing/locked/malformed transcript logs + skips, never
        breaking session end (the parser is itself fail-open; partial parses
        still emit their complete turns). Idempotent: the sidecar records
        emitted requestIds so a repeated SessionEnd skips them (first line of
        defence; the analyzer's (session_id, llm_turn_id) dedupe is the second).
        """
        if not ctx.transcript_path:
            logger.debug(
                "claude_code_hook: SessionEnd without transcript_path; "
                "no token batch"
            )
            return

        parsed = parse_transcript_file(ctx.transcript_path)
        already = read_emitted_turns(sink_path, ctx.session_id)
        newly_emitted: List[str] = []

        for request_id in parsed.get("turn_order", []):
            if request_id in already:
                continue
            turn = parsed["turns"][request_id]
            tokens = turn["tokens"]
            tool_use_ids = turn.get("tool_use_ids") or []
            # Skip turns with neither burn nor a tool call — nothing to add to
            # total OR attributable burn.
            if not turn.get("tokens_populated") and not tool_use_ids:
                continue
            # context_size_tokens (required int) = the turn's token footprint.
            burn = derive_turn_token_burn(tokens, turn.get("provider"))[
                "turn_token_burn"
            ]
            try:
                event = builder.build_token_snapshot(
                    llm_turn_id=request_id,
                    context_size_tokens=burn,
                    provider=turn.get("provider") or PROVIDER_ANTHROPIC,
                    model_identifier=turn.get("model_identifier"),
                    tool_use_ids=tool_use_ids or None,
                    provenance=["claude_code_transcript"],
                    **{f: tokens.get(f) for f in CANONICAL_TOKEN_FIELDS},
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "claude_code_hook: failed to build turn %s: %s",
                    request_id[:12],
                    exc,
                )
                continue
            if event:
                sink.write(event, ctx.session_id)
                newly_emitted.append(request_id)

        if newly_emitted:
            record_emitted_turns(sink_path, ctx.session_id, newly_emitted)

    # ------------------------------------------------------------------
    # Sidecar refresh
    # ------------------------------------------------------------------

    def _resolve_sink_path_for(self, session_id: str) -> Path:
        """Pick the sink file for this invocation.

        Priority order:

        1. Explicit ``sink_path`` passed at construction time —
           back-compat with legacy callers and tests that pin the
           exact file.
        2. Explicit ``sink_base`` + ``shared_file_mode`` supplied at
           construction time — caller-driven mode selection.
        3. Fall back to ``_resolve_sink_base()`` (env var or default).
        """
        if self._sink_path_override is not None:
            try:
                self._sink_path_override.parent.mkdir(
                    parents=True, exist_ok=True
                )
            except OSError:
                pass
            return self._sink_path_override
        if self._sink_base is not None and self._shared_file_mode is not None:
            base, shared = self._sink_base, self._shared_file_mode
        else:
            base, shared = _resolve_sink_base()
        return _session_file_for(base, shared, session_id)

    def _refresh_sidecar(
        self,
        session_manager: SessionManager,
        session_id: str,
        sink_path: Path,
    ) -> None:
        """After appending events, record the new end-state in the sidecar.

        We read the final ``(sequence, last_event_id)`` from the
        SessionManager entry directly — it was kept in sync as each
        event was emitted. The file offset is derived from the sink's
        current size minus the last line's length; rather than
        computing that, we record the offset of the last line via a
        lightweight backward scan (there will be at most a handful of
        lines just appended).
        """
        entry = session_manager._sessions.get(session_id)  # noqa: SLF001
        if entry is None or entry.last_event_id is None:
            return
        # Cheap offset retrieval: find the start of the last line by
        # reading the tail of the file. For a freshly appended line the
        # tail is small and bounded.
        try:
            size = file_size(sink_path)
            offset = self._find_last_line_start(sink_path, size)
        except OSError:
            return
        update_session_state(
            sink_path=sink_path,
            session_id=session_id,
            last_sequence=entry.sequence_number,
            last_event_id=entry.last_event_id,
            file_offset=offset,
        )

    def _find_last_line_start(self, sink_path: Path, file_end: int) -> int:
        """Return the offset of the first byte of the last non-empty line."""
        if file_end <= 0:
            return 0
        chunk_size = 4096
        with open(sink_path, "rb") as fh:
            # Scan back in 4KB chunks until we find a newline that is
            # NOT the terminating newline of the file.
            pos = file_end
            while pos > 0:
                read_from = max(0, pos - chunk_size)
                fh.seek(read_from)
                chunk = fh.read(pos - read_from)
                # Strip any trailing newline so we find the start of
                # the LAST line, not the newline that terminates it.
                end = len(chunk)
                if end > 0 and chunk[end - 1:end] == b"\n":
                    end -= 1
                nl = chunk.rfind(b"\n", 0, end)
                if nl != -1:
                    return read_from + nl + 1
                pos = read_from
            return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run_hook() -> int:
    """CLI entry. Reads JSON from stdin, processes, always returns 0.

    Claude Code interprets non-zero exit codes from PreToolUse hooks
    as a reason to abort the tool call (exit 2 specifically). The
    governance adapter must never interfere with tool execution, so
    we swallow every error and return 0.
    """
    try:
        raw = sys.stdin.read()
    except Exception as exc:
        print(f"sentience: stdin read failed: {exc}", file=sys.stderr)
        return 0

    if not raw.strip():
        # No payload — nothing to do (Claude Code may spawn hooks for
        # events we do not subscribe to if matchers are configured
        # liberally). Exit cleanly.
        return 0

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"sentience: malformed hook JSON: {exc}", file=sys.stderr)
        return 0

    if not isinstance(payload, dict):
        print("sentience: hook payload is not a JSON object", file=sys.stderr)
        return 0

    base, shared_file_mode = _resolve_sink_base()

    try:
        hook = ClaudeCodeGovernanceHook(
            payload,
            sink_base=base,
            shared_file_mode=shared_file_mode,
        )
        hook.process()
    except Exception as exc:  # pragma: no cover - belt-and-suspenders
        print(f"sentience: hook processing failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(run_hook())
