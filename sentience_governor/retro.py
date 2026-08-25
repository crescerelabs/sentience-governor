"""Retrospective Claude Code session reader (v0.3.1).

Reader is a retrospective GTM/discovery surface, architecturally quarantined
from runtime governance: it emits no ``GovernanceEvent``, writes no Sentience
trace, never mutates ``~/.sentience``, and takes no part in capture,
evaluation, enforcement or session management. The scan path performs zero
writes of any kind — transcripts are opened read-only and nothing else is
touched.

Evidence discipline (the short version; the plan is normative):

- The transcript format is owned by Claude Code and unversioned. Tolerate
  variation; a partial report beats an abort. Failures become counters.
- Bulk payloads (``content``, ``old_string``, ``new_string``,
  ``toolUseResult``) are never inspected and never outlive one loop
  iteration. ``lastPrompt`` / ``last-prompt`` records are never inspected,
  extracted, retained, analyzed or rendered.
- Target paths are interpreted lexically; a platform limitation must never
  produce a finding. Unknown is always safe.
- Bash commands are counted, never interpreted.
"""

from __future__ import annotations

import json
import os
import posixpath
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, NamedTuple, Optional

# Bounded reader limit: the maximum number of characters of one source line
# ever retained. ``readline(LIMIT + 1)`` limits characters, not exact UTF-8
# bytes, so the memory ceiling may be a small multiple of this nominal
# figure — the requirement is bounded memory, not an exact byte ceiling.
LIMIT = 4 * 1024 * 1024

_FILE_TOOLS = frozenset({"Read", "Write", "Edit"})
_WRITE_TOOLS = frozenset({"Write", "Edit"})
_DECLARE_PREFIX = "mcp__sentience__declare_intent"
_ACTIVITY_TYPES = frozenset({"user", "assistant"})

_SINCE_CHOICES = ("7d", "30d", "all")


class Finding(NamedTuple):
    """One session + one finding class + one normalized target (plan §3.1).

    Populated by the classification layer; repeated write activity to the
    same normalized target in the same session increments ``op_count``,
    never adds display rows.
    """

    session_id: str
    finding_class: str        # "claude_config" | "cross_project" | "non_project"
    target: str               # normalized target path
    tool: str                 # Write | Edit (last tool observed for the target)
    op_count: int             # write operations recorded against this target
    source_root: str          # "" when unresolved (never for cross_project)
    source_root_evidence: str  # "worktree" | "git" | "cwd-prefix" | "unknown"
    dest_root: str            # "" for non-project/config


def config_root() -> Path:
    """The discovery root: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def discover(root: Path) -> list[Path]:
    """All transcript files under ``root/projects``, sorted for stable order.

    Directory names are opaque grouping keys, never decoded; the per-record
    ``cwd`` is authoritative for where a session worked.
    """
    projects = Path(root) / "projects"
    if not projects.is_dir():
        return []
    return sorted(projects.rglob("*.jsonl"))


def interpret_target(file_path: object, cwd: object) -> tuple[Optional[str], str]:
    """Lexical target-path interpretation (plan §5.1). POSIX only.

    Returns ``(normalized_absolute_path, "ok")`` when the target is
    classifiable, else ``(None, reason)`` with reason in ``"missing"``,
    ``"windows"``, ``"home-relative"``, ``"env-var"``, ``"relative-no-cwd"``.
    Never consults the filesystem: historical strings must not be
    reinterpreted against today's state. Today's home is not historical
    evidence, so ``~`` and ``$VAR`` forms stay unknown.
    """
    if not isinstance(file_path, str) or not file_path:
        return None, "missing"
    if "\\" in file_path or (
        len(file_path) >= 2 and file_path[1] == ":" and file_path[0].isalpha()
    ):
        return None, "windows"
    if file_path.startswith("~"):
        return None, "home-relative"
    if "$" in file_path:
        return None, "env-var"
    if file_path.startswith("/"):
        return posixpath.normpath(file_path), "ok"
    # Relative: joined to that record's own cwd, which must itself be a
    # valid absolute POSIX path.
    if (
        not isinstance(cwd, str)
        or not cwd.startswith("/")
        or "\\" in cwd
        or "$" in cwd
    ):
        return None, "relative-no-cwd"
    return posixpath.normpath(posixpath.join(cwd, file_path)), "ok"


def _parse_since(since: str) -> Optional[timedelta]:
    """``7d`` / ``30d`` / ``all`` → window size, ``None`` meaning all."""
    if since == "all":
        return None
    if (
        isinstance(since, str)
        and since.endswith("d")
        and since[:-1].isdigit()
        and since[:-1]
    ):
        return timedelta(days=int(since[:-1]))
    raise ValueError(
        f"invalid --since value {since!r}: expected one of {', '.join(_SINCE_CHOICES)}"
    )


def _parse_timestamp(value: object) -> Optional[datetime]:
    """Record timestamp → aware datetime, or ``None`` (undated). Never guesses."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _bounded_lines(fh, agg: dict) -> Iterator[str]:
    """Bounded line reader (plan §4.1, verbatim contract).

    ``for line in fh`` materialises the entire line before any length check,
    so a single bulk payload could allocate without bound. This reader uses
    only the return value of ``readline``: at most ``LIMIT + 1`` characters
    of one line are ever retained, and an oversized record is drained in
    bounded chunks without ever reaching ``json.loads``.
    """
    while True:
        chunk = fh.readline(LIMIT + 1)

        if chunk == "":                       # normal EOF
            break

        if chunk.endswith("\n"):              # complete record
            agg["lines_total"] += 1
            yield chunk
            continue

        if len(chunk) <= LIMIT:               # final record, no trailing newline
            agg["lines_total"] += 1
            yield chunk                       # next readline returns "" → EOF
            continue

        # len(chunk) == LIMIT + 1 and no newline → record exceeds the limit.
        agg["lines_total"] += 1
        agg["lines_oversize"] += 1            # never parsed
        while True:                           # drain, bounded, retain nothing
            d = fh.readline(LIMIT)
            if d == "" or d.endswith("\n"):
                break


def _tool_use_blocks(record: dict) -> Iterator[dict]:
    """The ``tool_use`` blocks of an assistant record's ``message.content``.

    Tool requests live only on assistant records; results live on separate
    user records (``toolUseResult``), which Reader never inspects.
    """
    if record.get("type") != "assistant":
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def scan(
    root: Optional[os.PathLike] = None,
    since: str = "all",
    *,
    now: Optional[datetime] = None,
) -> dict:
    """Stream the transcript corpus and build the aggregate (plan §3.2).

    Read-only by construction. Memory is bounded by the per-record limit and
    the per-session aggregate, not by corpus size: each parsed record is
    projected to the extracted fields and dropped within its loop iteration.
    """
    t0 = time.monotonic()
    window = _parse_since(since)
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - window) if window is not None else None

    agg = {
        "files_scanned": 0,
        "files_unreadable": 0,
        "lines_total": 0,
        "lines_malformed": 0,
        "lines_oversize": 0,
        "records_undated": 0,
        "records_excluded_by_window": 0,
    }

    included_sessions: set[str] = set()
    by_session: dict[str, dict] = {}
    declaration_sessions: set[str] = set()
    titles: dict[str, str] = {}     # last observed custom-title wins
    slugs: dict[str, str] = {}      # first observed slug per session
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    tool_calls = 0
    file_ops = 0
    shell_calls = 0
    unknown_targets = 0
    unsupported_path_forms = 0

    for path in discover(Path(root) if root is not None else config_root()):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in _bounded_lines(fh, agg):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        agg["lines_malformed"] += 1
                        continue
                    if not isinstance(record, dict):
                        agg["lines_malformed"] += 1
                        continue

                    rtype = record.get("type")
                    session_id = record.get("sessionId")

                    if rtype == "custom-title":
                        # Metadata record: window-independent label
                        # extraction, never activity, never undated-counted.
                        title = record.get("customTitle")
                        if (
                            isinstance(session_id, str)
                            and session_id
                            and isinstance(title, str)
                            and title
                        ):
                            titles[session_id] = title
                        continue

                    if rtype not in _ACTIVITY_TYPES:
                        # Other metadata (last-prompt, queue-operation,
                        # attachment, system, unknown future types): never
                        # inspected beyond this type check.
                        continue

                    if not isinstance(session_id, str) or not session_id:
                        continue

                    # Label extraction is window-independent metadata.
                    slug = record.get("slug")
                    if isinstance(slug, str) and slug and session_id not in slugs:
                        slugs[session_id] = slug

                    timestamp = _parse_timestamp(record.get("timestamp"))
                    if timestamp is None and any(
                        True for _ in _tool_use_blocks(record)
                    ):
                        # A coverage characteristic, counted in every window
                        # mode: the window governs whether a record
                        # participates, never whether its missing timestamp
                        # is counted. Scoped to records carrying tool
                        # activity, so structurally-undated metadata cannot
                        # flood the coverage line.
                        agg["records_undated"] += 1
                    if cutoff is not None:
                        if timestamp is None:
                            continue    # never guessed into a finite window
                        if timestamp < cutoff:
                            agg["records_excluded_by_window"] += 1
                            continue

                    included_sessions.add(session_id)
                    if timestamp is not None:
                        if period_start is None or timestamp < period_start:
                            period_start = timestamp
                        if period_end is None or timestamp > period_end:
                            period_end = timestamp

                    counts = by_session.setdefault(
                        session_id,
                        {"tool_calls": 0, "file_ops": 0, "shell_calls": 0},
                    )
                    for block in _tool_use_blocks(record):
                        name = block.get("name")
                        tool_calls += 1
                        counts["tool_calls"] += 1
                        if not isinstance(name, str):
                            continue
                        if name == "Bash":
                            # Counted, never interpreted (§6).
                            shell_calls += 1
                            counts["shell_calls"] += 1
                        elif name in _FILE_TOOLS:
                            file_ops += 1
                            counts["file_ops"] += 1
                            block_input = block.get("input")
                            file_path = (
                                block_input.get("file_path")
                                if isinstance(block_input, dict)
                                else None
                            )
                            target, reason = interpret_target(
                                file_path, record.get("cwd")
                            )
                            if target is None:
                                unknown_targets += 1
                                if reason == "windows":
                                    unsupported_path_forms += 1
                        if name.startswith(_DECLARE_PREFIX):
                            declaration_sessions.add(session_id)
                    # ``record`` is dropped here: bulk payloads never
                    # outlive one loop iteration.
            agg["files_scanned"] += 1
        except OSError:
            agg["files_unreadable"] += 1

    session_labels = {}
    for session_id in included_sessions:
        if session_id in titles:
            label, source = titles[session_id], "custom-title"
        elif session_id in slugs:
            label, source = slugs[session_id], "slug"
        else:
            label, source = session_id[:8], "session-id"
        session_labels[session_id] = {"label": label, "source": source}

    by_session = {
        sid: counts for sid, counts in by_session.items()
        if sid in included_sessions
    }

    agg.update({
        "sessions": len(included_sessions),
        "sessions_with_tools": sum(
            1 for counts in by_session.values() if counts["tool_calls"] > 0
        ),
        "sessions_with_declaration_activity": sorted(declaration_sessions),
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "tool_calls": tool_calls,
        "file_ops": file_ops,
        "shell_calls": shell_calls,
        "by_session": by_session,
        "unknown_targets": unknown_targets,
        "unsupported_path_forms": unsupported_path_forms,
        "session_labels": session_labels,
        # Classification layer (plan §5) — populated by the classifier.
        "suppressed": {},
        "unknown_root_writes": 0,
        "outside_cwd_secondary": 0,
        "same_project_writes": 0,
        "sessions_with_findings": [],
        "findings": [],
        "findings_omitted": 0,
        "targets_omitted": 0,
        "scan_seconds": time.monotonic() - t0,
        "since": since,
    })
    return agg
