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

# Collection bound. Overflow increments ``targets_omitted``; the report says
# so, because past this point collection itself is scan-order-bounded.
MAX_TARGETS = 10_000

# Display cap on findings (§8.4). Ranking is computed before truncation and
# `findings_omitted` states what display dropped.
MAX_DISPLAY_FINDINGS = 500

# Defensive belt-and-braces on the parent walk. The walk is a finite lexical
# parent chain and cannot cycle by construction, but the cap means that
# property does not depend on the implementer avoiding ``resolve()``.
_DEPTH_CAP = 64

_WORKTREE_MARKER = "/.claude/worktrees/"


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


def _is_within(path: str, base: str) -> bool:
    """Component-boundary containment: ``path`` equals or sits under ``base``.

    Pure string comparison — ``/a/proj`` never claims ``/a/proj-extras``.
    """
    return path == base or path.startswith(base.rstrip("/") + "/")


def _relative_components(path: str, base: str) -> Optional[list[str]]:
    """Components of ``path`` below ``base``, or ``None`` if not below it."""
    base = base.rstrip("/")
    if not path.startswith(base + "/"):
        return None
    return [c for c in path[len(base) + 1:].split("/") if c]


def suppression_rule(target: str, root: str, home: str) -> Optional[str]:
    """The named suppression rule matching ``target``, if any (plan §5.3).

    Five narrow named rules rather than a blanket ``~/.claude/**``: the
    measured noise came from auto-maintained classes only, and a future
    Claude Code auto-directory must fail visibly (an unexplained finding)
    rather than silently (swallowed by a wildcard). Applied *before* root
    resolution, so suppressed paths cost no filesystem work and pollute no
    count except their own rule's.
    """
    rel = _relative_components(target, root)
    if rel:
        # S1 — <root>/projects/*/memory/**  (auto-maintained memory)
        if len(rel) >= 4 and rel[0] == "projects" and rel[2] == "memory":
            return "S1"
        # S2 — <root>/projects/*/*/tool-results/**  (tool-result cache)
        if len(rel) >= 5 and rel[0] == "projects" and rel[3] == "tool-results":
            return "S2"
        # S3 — <root>/projects/**/*.jsonl  (the transcripts themselves)
        if len(rel) >= 2 and rel[0] == "projects" and rel[-1].endswith(".jsonl"):
            return "S3"
        # S4 — <root>/plans/**  (agent-authored plan scratch)
        if len(rel) >= 2 and rel[0] == "plans":
            return "S4"
    # S5 — /tmp/claude-*/** and /private/tmp/claude-*/**  (harness scratchpad)
    for base in ("/tmp", "/private/tmp"):
        rel = _relative_components(target, base)
        if rel and len(rel) >= 2 and rel[0].startswith("claude-"):
            return "S5"
    return None


def is_claude_config(target: str, root: str, home: str) -> bool:
    """Whether ``target`` is a known Claude control surface (plan §5.2).

    A narrow *positive* list, not "everything under ~/.claude that is not
    suppressed": classifying by positive list is what stops a new Claude
    Code auto-directory from reintroducing infrastructure noise as the
    strongest finding class. Anything else under the config root falls
    through to ordinary resolution and weaker context — never rank 1.

    Anchored at the *current* home (and ``$CLAUDE_CONFIG_DIR`` where it
    overrides discovery). Transcripts recorded under a different historical
    home do not match and fall through; guessing historical homes would
    manufacture evidence.
    """
    if target == posixpath.join(home, ".claude.json"):
        return True
    rel = _relative_components(target, root)
    if not rel:
        return False
    if rel == ["settings.json"]:
        return True
    return len(rel) >= 2 and rel[0] in ("hooks", "skills", "commands", "agents")


class RootResolver:
    """Project-root resolution (plan §5.4) — layer 2, filesystem-informed.

    Deliberately quarantined from ``hook_config._git_root``: different stop
    rules (``$HOME``), memoization, and a worktree step. This layer is
    best-effort retrospective evidence about project identity *today*, not
    historical fact; when identity cannot be established the claim is
    suppressed rather than manufactured (§5.5).
    """

    def __init__(self, home: str):
        self._home = home.rstrip("/")
        self._memo: dict[str, Optional[str]] = {}
        self.probed: list[str] = []

    def _git_root(self, directory: str) -> Optional[str]:
        """Step 2: nearest ancestor containing ``.git``, memoized per directory.

        Walks strictly below ``$HOME`` — ``$HOME`` itself is never checked
        and never returned, so a dotfiles repository at ``~/.git`` cannot
        collapse every home path into one project. For paths outside
        ``$HOME`` the walk stops at ``/`` without checking it. The chain is
        a finite lexical parent walk: no symlink is followed during
        traversal, so it strictly shortens toward its stop boundary.
        """
        if directory in self._memo:
            return self._memo[directory]

        chain: list[str] = []
        found: Optional[str] = None
        current = directory
        for _ in range(_DEPTH_CAP):
            if current in self._memo:
                found = self._memo[current]
                break
            if current in ("", "/") or current == self._home:
                break
            chain.append(current)
            self.probed.append(current)
            # `.git` as file or directory: a user worktree resolves to
            # itself, the defensible answer without reading `.git`.
            if os.path.exists(os.path.join(current, ".git")):
                found = current
                break
            parent = posixpath.dirname(current)
            if parent == current:
                break
            current = parent

        for entry in chain:
            self._memo[entry] = found
        return found

    def resolve(
        self, directory: str, session_cwds: Optional[set] = None
    ) -> tuple[str, str]:
        """Resolve ``directory`` to ``(root, evidence)``; ``("", "unknown")``
        when no root can be established."""
        if not directory or not directory.startswith("/"):
            return "", "unknown"

        # Step 1 — harness worktree, lexical. Untreated, every worktree
        # session becomes a false cross-project source (c14).
        marker = directory.find(_WORKTREE_MARKER)
        if marker != -1:
            return directory[:marker], "worktree"

        # Step 2 — filesystem `.git` evidence.
        git_root = self._git_root(directory)
        if git_root is not None:
            return git_root, "git"

        # Step 3 — transcript-only fallback: the shortest observed session
        # cwd that is a prefix of the path at a component boundary.
        if session_cwds:
            candidates = [
                cwd for cwd in session_cwds if _is_within(directory, cwd)
            ]
            if candidates:
                return min(candidates, key=lambda c: (len(c), c)), "cwd-prefix"

        # Step 4 — UNKNOWN. Never a fabricated root.
        return "", "unknown"


# Finding-class precedence (§8.4): claude_config, then cross_project, then
# non_project. No numerical risk score, probability, severity model or
# confidence percentage exists anywhere in Reader.
_CLASS_RANK = {"claude_config": 0, "cross_project": 1, "non_project": 2}


def _label_of(session_labels: dict, session_id: str) -> str:
    entry = session_labels.get(session_id)
    return entry["label"] if entry else session_id


def rank_findings(findings: list, session_labels: dict) -> list:
    """Order findings per §8.4 — a total order, so rendering is reproducible.

    (1) finding class; (2) within class, distinct-target count per
    (session, dest root) descending; (3) op_count descending; (4) session
    label, then target, ascending.
    """
    distinct: dict = {}
    for finding in findings:
        key = (finding.session_id, finding.dest_root)
        distinct[key] = distinct.get(key, 0) + 1

    def sort_key(finding):
        return (
            _CLASS_RANK.get(finding.finding_class, len(_CLASS_RANK)),
            -distinct[(finding.session_id, finding.dest_root)],
            -finding.op_count,
            _label_of(session_labels, finding.session_id),
            finding.target,
        )

    return sorted(findings, key=sort_key)


def rank_sessions(findings: list, session_labels: dict) -> list:
    """Order standout sessions per §8.4 — State N's rows and detail order.

    (1) best finding class present in the session; (2) total distinct
    targets across the session's findings, descending; (3) total op_count,
    descending; (4) session label ascending.
    """
    summary: dict = {}
    for finding in findings:
        entry = summary.setdefault(
            finding.session_id,
            {"best": len(_CLASS_RANK), "targets": set(), "ops": 0},
        )
        entry["best"] = min(
            entry["best"], _CLASS_RANK.get(finding.finding_class, len(_CLASS_RANK))
        )
        entry["targets"].add(finding.target)
        entry["ops"] += finding.op_count

    def sort_key(session_id):
        entry = summary[session_id]
        return (
            entry["best"],
            -len(entry["targets"]),
            -entry["ops"],
            _label_of(session_labels, session_id),
        )

    return sorted(summary, key=sort_key)


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

    discovery_root = Path(root) if root is not None else config_root()
    root_str = str(discovery_root).rstrip("/")
    home_str = str(Path.home()).rstrip("/")
    session_cwds: dict[str, set] = {}
    suppressed: dict[str, int] = {}
    outside_cwd_secondary = 0
    targets_omitted = 0
    # Collected per (session, target, cwd): the finding class is not known
    # until root resolution, which needs every cwd the session was observed
    # at (§5.4 step 3) and therefore cannot run during streaming.
    write_events: dict[tuple, list] = {}

    for path in discover(discovery_root):
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
                    record_cwd = record.get("cwd")
                    if isinstance(record_cwd, str) and record_cwd.startswith("/"):
                        record_cwd = posixpath.normpath(record_cwd)
                        session_cwds.setdefault(session_id, set()).add(record_cwd)
                    else:
                        record_cwd = None
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
                            elif name in _WRITE_TOOLS:
                                # Reads are never findings and never enter
                                # the secondary count; they contribute only
                                # to file_ops (§5.2, critic finding 8).
                                rule = suppression_rule(
                                    target, root_str, home_str
                                )
                                if rule is not None:
                                    suppressed[rule] = suppressed.get(rule, 0) + 1
                                elif (
                                    record_cwd is None
                                    or not _is_within(target, record_cwd)
                                ):
                                    outside_cwd_secondary += 1
                                if rule is None:
                                    key = (session_id, target, record_cwd or "")
                                    event = write_events.get(key)
                                    if event is not None:
                                        event[0] += 1
                                        event[1] = name
                                    elif len(write_events) < MAX_TARGETS:
                                        write_events[key] = [1, name]
                                    else:
                                        targets_omitted += 1
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

    # Classification (§5.2/§5.5). Deferred to here because step 3 of root
    # resolution needs every cwd observed for the session.
    resolver = RootResolver(home_str)
    findings_by_key: dict[tuple, list] = {}
    unknown_root_writes = 0
    same_project_writes = 0

    for (session_id, target, cwd), (op_count, tool) in write_events.items():
        cwds = session_cwds.get(session_id)
        source_root, source_evidence = (
            resolver.resolve(cwd, cwds) if cwd else ("", "unknown")
        )

        if is_claude_config(target, root_str, home_str):
            # Rank 1, and source-independent: the governance-relevant fact
            # is that Claude targeted its own global configuration, which
            # can affect future Claude sessions.
            finding_class, dest_root = "claude_config", ""
        elif not source_root:
            # Without knowing where the session worked, "another project"
            # cannot be asserted. The UNKNOWN-side rule is asymmetric by
            # design (§5.5): a source-side UNKNOWN suppresses the claim.
            unknown_root_writes += op_count
            continue
        else:
            dest_root, _ = resolver.resolve(posixpath.dirname(target), cwds)
            if not dest_root:
                # Honestly covers both a genuine non-project directory and
                # a repository since moved or deleted.
                finding_class = "non_project"
            elif dest_root == source_root:
                same_project_writes += op_count
                continue
            else:
                finding_class = "cross_project"

        key = (session_id, finding_class, target)
        existing = findings_by_key.get(key)
        if existing is None:
            findings_by_key[key] = [
                op_count, tool, source_root, source_evidence, dest_root,
            ]
        else:
            existing[0] += op_count
            existing[1] = tool

    findings = [
        Finding(
            session_id=session_id,
            finding_class=finding_class,
            target=target,
            tool=tool,
            op_count=op_count,
            source_root=source_root,
            source_root_evidence=source_evidence,
            dest_root=dest_root,
        )
        for (session_id, finding_class, target), (
            op_count, tool, source_root, source_evidence, dest_root
        ) in sorted(findings_by_key.items())
    ]

    # Ranking runs over the full collected aggregate, then display
    # truncates — display truncation can never masquerade as "strongest
    # findings" (§8.4).
    findings = rank_findings(findings, session_labels)
    ranked_sessions = rank_sessions(findings, session_labels)
    findings_omitted = max(0, len(findings) - MAX_DISPLAY_FINDINGS)
    findings = findings[:MAX_DISPLAY_FINDINGS]

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
        "suppressed": dict(sorted(suppressed.items())),
        "unknown_root_writes": unknown_root_writes,
        "outside_cwd_secondary": outside_cwd_secondary,
        "same_project_writes": same_project_writes,
        "sessions_with_findings": ranked_sessions,
        "findings": findings,
        "findings_omitted": findings_omitted,
        "targets_omitted": targets_omitted,
        "scan_seconds": time.monotonic() - t0,
        "since": since,
        # Private renderer metadata, not part of the §3.2 public contract:
        # carried so the renderer can abbreviate paths for display without
        # reading the environment (renderers are contractually pure). The
        # leading underscore marks it private and `json_payload` never
        # emits it.
        "_home": home_str,
    })
    return agg


# The §3.2 aggregate — the public `--json` contract, in plan order. Private
# renderer metadata (anything underscore-prefixed) is deliberately absent:
# an implementation detail must not become public API.
PUBLIC_FIELDS = (
    "files_scanned", "files_unreadable",
    "lines_total", "lines_malformed", "lines_oversize",
    "records_undated", "records_excluded_by_window",
    "sessions", "sessions_with_tools",
    "sessions_with_declaration_activity",
    "period_start", "period_end",
    "tool_calls", "file_ops", "shell_calls",
    "by_session",
    "unknown_targets", "unsupported_path_forms",
    "session_labels",
    "suppressed", "unknown_root_writes",
    "outside_cwd_secondary", "same_project_writes",
    "sessions_with_findings", "findings",
    "findings_omitted", "targets_omitted",
    "scan_seconds", "since",
)


def json_payload(result: dict) -> dict:
    """The §3.2 aggregate as JSON-serializable data (``--json``).

    Emits exactly the §3.2 public fields — never private renderer
    metadata. Sets and tuples become sorted lists and objects; field names
    say what they are: ``op_count`` counts *recorded operations* (§7.2).
    """
    payload = {}
    for key in PUBLIC_FIELDS:
        if key not in result:
            continue
        value = result[key]
        if key == "findings":
            payload[key] = [finding._asdict() for finding in value]
        elif isinstance(value, (set, frozenset)):
            payload[key] = sorted(value)
        elif isinstance(value, tuple):
            payload[key] = list(value)
        else:
            payload[key] = value
    return payload
