"""Claude Code hook-configuration convergence (v0.3.0.3).

One convergence engine — one classifier, one planner, one applier — shared by
``sentience init claude-code`` and the on-use seam in ``cli.ux.main()``.
"First install", "upgrade migration" and "repair" name the *input state*
presented to this engine, not separate workflows.

The governing invariant:

    After Sentience has valid project context, Sentience-managed Claude Code
    configuration equals the canonical configuration required by the running
    install.

Canonical home (v0.3.0.3+): the machine-local ``.claude/settings.local.json``.
The team-shared ``.claude/settings.json`` is READ-ONLY migration evidence —
nothing in this module ever writes it.

Supported floor: Claude Code v2.1.211+ (declared, not detected). The
``settings.local.json`` repository-root resolution mirrored here is documented
Claude Code behaviour from that version.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The three hook events Sentience wires (unchanged since v0.2.6.1).
GOVERNED_EVENTS: Tuple[str, ...] = ("PreToolUse", "PostToolUse", "SessionEnd")

#: Basename of the console script the canonical entry invokes.
HOOK_BASENAME = "sentience-claude-code-hook"

#: Substring tokens marking a command as Sentience-looking (AMBIGUOUS when the
#: entry is not exactly MANAGED). A false AMBIGUOUS costs a warning; a false
#: FOREIGN costs double capture — so the test is deliberately broad.
AMBIGUOUS_TOKENS: Tuple[str, ...] = (
    "sentience-claude-code-hook",
    "sentience_governor",
    "sentience-governor",
)

#: Sentinel for "the file did not exist when read" in the lost-update compare.
ABSENT = object()

# Outcome codes returned by converge().
NOOP = "noop"                        # nothing to do (silent)
CREATED = "created"                  # local file created with canonical config
UPDATED = "updated"                  # existing local file converged
AMBIGUOUS_LOCAL = "ambiguous_local"  # modified Sentience-looking local entry
SHARED_CONFLICT = "shared_conflict"  # live conflicting/ambiguous shared entry
NO_BINARY = "no_binary"              # running install has no hook binary
BINARY_INVALID = "binary_invalid"    # resolved binary fails verification
UNREADABLE = "unreadable"            # a settings file could not be read
MALFORMED = "malformed"              # a settings file is not valid JSON
UNWRITABLE = "unwritable"            # the write failed
WRITE_CONFLICT = "write_conflict"    # lost-update guard aborted the write


def _is_posix() -> bool:
    return os.name == "posix"


def _stderr_isatty() -> bool:
    """TTY gate for warnings (§9). Isolated so tests can monkeypatch it."""
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

def command_is_managed(command: Any, posix: Optional[bool] = None) -> bool:
    """§5.2 — the whole string is one path, never tokenized.

    ``basename(command)``, with one trailing ``.exe`` stripped, must equal
    ``sentience-claude-code-hook``. Case-sensitive on POSIX; case-insensitive
    on non-POSIX (a miscased ``.EXE`` self-entry must not be misclassified).
    Paths containing spaces and parentheses therefore match correctly.
    """
    if not isinstance(command, str) or not command:
        return False
    if posix is None:
        posix = _is_posix()
    base = os.path.basename(command)
    if posix:
        if base.endswith(".exe"):
            base = base[: -len(".exe")]
        return base == HOOK_BASENAME
    low = base.lower()
    if low.endswith(".exe"):
        low = low[: -len(".exe")]
    return low == HOOK_BASENAME


def classify_entry(entry: Any, posix: Optional[bool] = None) -> str:
    """Classify one outer hook entry: ``"managed"``, ``"ambiguous"``, ``"foreign"``.

    §5.1 MANAGED — the entire outer entry is structurally identical to what
    ``_hook_entry()`` generates: key set exactly ``{"matcher", "hooks"}``,
    ``matcher == ""``, ``hooks`` a list of exactly one object with key set
    exactly ``{"type", "command"}``, ``type == "command"``, and a command
    matching §5.2. A backwards-compatibility inference, not provenance.

    §5.3 AMBIGUOUS — not MANAGED, and any inner command string, lowercased,
    *contains* one of AMBIGUOUS_TOKENS. No tokenization, no parser.

    Otherwise FOREIGN — never inspected further, never modified.
    """
    if isinstance(entry, dict) and set(entry.keys()) == {"matcher", "hooks"}:
        hooks = entry.get("hooks")
        if (
            entry.get("matcher") == ""
            and isinstance(hooks, list)
            and len(hooks) == 1
            and isinstance(hooks[0], dict)
            and set(hooks[0].keys()) == {"type", "command"}
            and hooks[0].get("type") == "command"
            and command_is_managed(hooks[0].get("command"), posix)
        ):
            return "managed"

    # Not managed: substring scan of every inner command string we can reach.
    for cmd in _inner_commands(entry):
        low = cmd.lower()
        if any(tok in low for tok in AMBIGUOUS_TOKENS):
            return "ambiguous"
    return "foreign"


def _inner_commands(entry: Any) -> List[str]:
    """Every string command reachable under ``entry["hooks"][*]["command"]``."""
    out: List[str] = []
    if isinstance(entry, dict):
        hooks = entry.get("hooks")
        if isinstance(hooks, list):
            for h in hooks:
                if isinstance(h, dict) and isinstance(h.get("command"), str):
                    out.append(h["command"])
    return out


# ---------------------------------------------------------------------------
# Liveness (§4.2 step 9 / §7.1 — per-class; §21 final table)
# ---------------------------------------------------------------------------

def _verify_path(path: str, posix: Optional[bool] = None) -> bool:
    """§7.1 — POSIX: is_file and X_OK; non-POSIX: is_file only."""
    if posix is None:
        posix = _is_posix()
    try:
        p = Path(path)
        if not p.is_file():
            return False
        if posix and not os.access(path, os.X_OK):
            return False
        return True
    except Exception:
        return False


def managed_entry_live(command: str, posix: Optional[bool] = None) -> bool:
    """Liveness for a MANAGED command.

    ``expanduser`` first. Absolute after expansion → verify the WHOLE string
    as one path (never tokenized: tokenizing would fragment a spaced path and
    misclassify a live entry as dead). Not absolute (bare name, relative) →
    LIVE by fiat: it may resolve via PATH in the hook's shell, and we cannot
    prove it dead. Sentience itself always writes absolute paths, so a
    non-absolute MANAGED entry is hand-authored; erring toward live errs
    toward blocking, the safe direction.

    AMBIGUOUS entries never reach this function — they are LIVE by fiat,
    always (§21): liveness is never computed for an entry we cannot parse.
    """
    expanded = os.path.expanduser(command)
    if not os.path.isabs(expanded):
        return True
    return _verify_path(expanded, posix)


# ---------------------------------------------------------------------------
# Project resolution (§6)
# ---------------------------------------------------------------------------

def shared_settings_path(project_dir: Path) -> Path:
    """§6.1 — the shared file is read from the starting directory only.

    Claude Code reads ``.claude/settings.json`` only from the folder you start
    in. No upward walk, no git-root resolution.
    """
    return Path(project_dir) / ".claude" / "settings.json"


def resolve_local_settings_path(project_dir: Path) -> Path:
    """§6.2 — mirror Claude Code v2.1.211+ resolution for ``settings.local.json``.

    Repository root, EXCEPT: outside a git repo · the repo root is ``$HOME`` ·
    non-POSIX (deliberate conservative superset of the documented "on
    Windows") · the repo root or its ``.git``/``.claude`` is not owned by the
    current user. In each exception the file stays in the starting directory.
    """
    project_dir = Path(project_dir)
    fallback = project_dir / ".claude" / "settings.local.json"

    if not _is_posix():
        return fallback

    root = _git_root(project_dir)
    if root is None:
        return fallback
    try:
        if root == Path.home():
            return fallback
    except Exception:
        return fallback
    if not _owned_by_current_user(root):
        return fallback
    git_entry = root / ".git"
    if git_entry.exists() and not _owned_by_current_user(git_entry):
        return fallback
    claude_entry = root / ".claude"
    if claude_entry.exists() and not _owned_by_current_user(claude_entry):
        return fallback
    return root / ".claude" / "settings.local.json"


def _git_root(start: Path) -> Optional[Path]:
    """Nearest ancestor (including ``start``) containing ``.git``; None outside
    a repository. Never walks above the filesystem root."""
    try:
        cur = Path(start).resolve()
    except Exception:
        return None
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def _owned_by_current_user(path: Path) -> bool:
    try:
        return path.stat().st_uid == os.getuid()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Reading (§4.2 steps 1–2)
# ---------------------------------------------------------------------------

@dataclass
class _FileState:
    doc: Optional[Dict[str, Any]]   # parsed document; {} when absent
    state: str                      # "ok" | "absent" | "unreadable" | "malformed"
    raw: Any                        # bytes snapshot, or ABSENT
    reason: str = ""


def read_settings(path: Path) -> _FileState:
    """Read one settings file. Absent → empty document. Unreadable or
    malformed → state UNKNOWN (never guessed at, never mutated)."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return _FileState(doc={}, state="absent", raw=ABSENT)
    except OSError as exc:
        return _FileState(doc=None, state="unreadable", raw=None, reason=str(exc))
    try:
        text = raw.decode("utf-8")
        doc = json.loads(text) if text.strip() else {}
        if not isinstance(doc, dict):
            return _FileState(doc=None, state="malformed", raw=raw,
                              reason="top level is not a JSON object")
        return _FileState(doc=doc, state="ok", raw=raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _FileState(doc=None, state="malformed", raw=raw, reason=str(exc))


def _entries_by_class(doc: Dict[str, Any], posix: Optional[bool] = None
                      ) -> Dict[str, Dict[str, List[Tuple[int, Any]]]]:
    """Classify every entry in the three governed events.

    Returns {event: {"managed"|"ambiguous"|"foreign": [(index, entry), ...]}}.
    Only the three governed events are inspected (A12).
    """
    out: Dict[str, Dict[str, List[Tuple[int, Any]]]] = {}
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    for event in GOVERNED_EVENTS:
        buckets: Dict[str, List[Tuple[int, Any]]] = {
            "managed": [], "ambiguous": [], "foreign": []
        }
        entries = hooks.get(event) if isinstance(hooks, dict) else None
        if isinstance(entries, list):
            for i, e in enumerate(entries):
                buckets[classify_entry(e, posix)].append((i, e))
        out[event] = buckets
    return out


def _managed_command(entry: Dict[str, Any]) -> str:
    return entry["hooks"][0]["command"]


def canonical_entry(command: str) -> Dict[str, Any]:
    """The canonical hook entry shape — identical to ``ux._hook_entry()``."""
    return {"matcher": "", "hooks": [{"type": "command", "command": command}]}


# ---------------------------------------------------------------------------
# Planner (§4.2 steps 3, 5, 8–12 — pure; no filesystem access)
# ---------------------------------------------------------------------------

@dataclass
class PlanResult:
    outcome: str                     # NOOP | AMBIGUOUS_LOCAL | SHARED_CONFLICT | "plan"
    new_local: Optional[Dict[str, Any]] = None
    evidence: bool = False
    detail: str = ""                 # e.g. "PreToolUse[1]" for ambiguous local


def plan_convergence(
    local_doc: Dict[str, Any],
    shared_doc: Dict[str, Any],
    binary: str,
    may_create_without_evidence: bool,
    caller_is_seam: bool,
    posix: Optional[bool] = None,
) -> PlanResult:
    """The pure planner. Both callers use exactly this function; they differ
    only in the two flags (§4.1) and in how outcomes are rendered."""
    local_cls = _entries_by_class(local_doc, posix)
    shared_cls = _entries_by_class(shared_doc, posix)

    # Step 3 — evidence: any MANAGED or AMBIGUOUS entry in either file.
    evidence = any(
        cls_map[ev][k]
        for cls_map in (local_cls, shared_cls)
        for ev in GOVERNED_EVENTS
        for k in ("managed", "ambiguous")
    )

    # Step 5 — never turn a project with zero Sentience evidence into a
    # configured project from the seam (I6, rule 1).
    if not _evidence_gate(evidence, may_create_without_evidence):
        return PlanResult(outcome=NOOP, evidence=False)

    # Step 8 — ambiguous LOCAL entry: stop before the rule-2 NOOP below can
    # mask it (gate-2 finding 2).
    for event in GOVERNED_EVENTS:
        amb = local_cls[event]["ambiguous"]
        if amb:
            return PlanResult(outcome=AMBIGUOUS_LOCAL, evidence=evidence,
                              detail=f"{event}[{amb[0][0]}]")

    # Step 9 — the SET of live shared Sentience entries, per-class liveness.
    live_managed_cmds: List[str] = []
    live_has_ambiguous = False
    for event in GOVERNED_EVENTS:
        for _i, e in shared_cls[event]["managed"]:
            if managed_entry_live(_managed_command(e), posix):
                live_managed_cmds.append(_managed_command(e))
        if shared_cls[event]["ambiguous"]:
            live_has_ambiguous = True  # LIVE by fiat, always (§21)

    if _shared_live_conflict(live_has_ambiguous, live_managed_cmds, binary):
        return PlanResult(outcome=SHARED_CONFLICT, evidence=evidence)

    # Step 10 — rule-2 NOOP, scoped to the seam: a healthy live shared hook
    # equal to canonical, with no local Sentience configuration, is left
    # alone. Explicit init proceeds and creates the identical local entry
    # (Claude Code de-duplicates identical handlers).
    local_has_sentience = any(
        local_cls[ev][k] for ev in GOVERNED_EVENTS for k in ("managed", "ambiguous")
    )
    if (
        caller_is_seam
        and live_managed_cmds
        and all(c == binary for c in live_managed_cmds)
        and not local_has_sentience
    ):
        return PlanResult(outcome=NOOP, evidence=evidence)

    # Step 11 — per governed event, over local: record the index of the first
    # MANAGED entry (else end of list), remove all MANAGED entries, insert
    # exactly one canonical entry at that index. FOREIGN entries keep their
    # relative order (A13/I4).
    new_local = json.loads(json.dumps(local_doc)) if local_doc else {}
    hooks = new_local.setdefault("hooks", {})
    for event in GOVERNED_EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
        hooks[event] = _converge_event_entries(entries, binary, posix)

    # Step 12 — the fixed point is over the NET result (deep equality), not
    # over whether remove/insert operations were enumerated.
    if new_local == local_doc:
        return PlanResult(outcome=NOOP, evidence=evidence)
    return PlanResult(outcome="plan", new_local=new_local, evidence=evidence)


def _evidence_gate(evidence: bool, may_create_without_evidence: bool) -> bool:
    """§4.2 step 5 — proceed iff evidence exists, or the caller may create
    without it (init only). Isolated as a named rule so mutation test 37 can
    target exactly this guard."""
    return evidence or may_create_without_evidence


def _shared_live_conflict(live_has_ambiguous: bool,
                          live_managed_cmds: List[str], binary: str) -> bool:
    """§4.2 step 9 conflict rule, quantified over the SET of live entries:
    one live equal entry never masks a live differing or ambiguous one.
    Isolated so mutation test 46 can target exactly this guard."""
    return live_has_ambiguous or any(c != binary for c in live_managed_cmds)


def _converge_event_entries(entries: List[Any], binary: str,
                            posix: Optional[bool] = None) -> List[Any]:
    """§4.2 step 11 for one event: record the index of the first MANAGED
    entry (else end of list), remove ALL managed entries, insert exactly one
    canonical entry at that index. Foreign/ambiguous entries keep their
    relative order (A13/I4). Isolated so mutation test 36 can target exactly
    the duplicate collapse."""
    managed_idx = [i for i, e in enumerate(entries)
                   if classify_entry(e, posix) == "managed"]
    insert_at = managed_idx[0] if managed_idx else len(entries)
    kept = [e for i, e in enumerate(entries) if i not in managed_idx]
    # Everything kept that originally sat before the first managed entry
    # stays before the canonical insertion.
    before = sum(1 for i in range(len(entries))
                 if i < insert_at and i not in managed_idx)
    kept.insert(before, canonical_entry(binary))
    return kept


# ---------------------------------------------------------------------------
# Applier (§4.4 — torn-write and lost-update protection)
# ---------------------------------------------------------------------------

def apply_plan(local_path: Path, snapshot_raw: Any,
               new_doc: Dict[str, Any]) -> Tuple[str, str]:
    """Write ``new_doc`` to ``local_path`` under the safe-write contract.

    Returns ``(status, reason)`` where status is CREATED, UPDATED,
    WRITE_CONFLICT or UNWRITABLE. Torn writes: temp file in the same
    directory, flush + fsync, ``os.replace``; the temp file is unlinked on any
    failure. Lost updates: re-read immediately before replace and abort on ANY
    difference from the snapshot — ``ABSENT -> present`` counts as a
    difference. Catches ``Exception`` only, never ``BaseException``.
    """
    local_path = Path(local_path)
    payload = (json.dumps(new_doc, indent=2) + "\n").encode("utf-8")
    tmp_name = None
    try:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".sentience-settings-", dir=str(local_path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            # Lost-update guard: compare the target NOW against the snapshot
            # taken at read time. Any difference aborts; convergence is
            # idempotent, so the next invocation retries safely.
            current = _reread_for_compare(local_path)
            if not _snapshots_equal(snapshot_raw, current):
                return (WRITE_CONFLICT,
                        "file changed since it was read; not overwriting")
            os.replace(tmp_name, local_path)
            tmp_name = None
            return (CREATED if snapshot_raw is ABSENT else UPDATED, "")
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except Exception:
                    pass
    except Exception as exc:  # never BaseException: ^C must not be swallowed
        return (UNWRITABLE, str(exc))


def _reread_for_compare(path: Path) -> Any:
    """Target bytes immediately before replace, or ABSENT. Isolated so tests
    can monkeypatch the lost-update race deterministically."""
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return ABSENT
    except OSError:
        return None  # unreadable now -> treated as a difference


def _snapshots_equal(a: Any, b: Any) -> bool:
    if a is ABSENT or b is ABSENT:
        return a is ABSENT and b is ABSENT
    if a is None or b is None:
        return False
    return a == b


# ---------------------------------------------------------------------------
# The engine (§4.2 — full algorithm, I/O included)
# ---------------------------------------------------------------------------

@dataclass
class ConvergeResult:
    outcome: str
    local_path: Optional[Path] = None
    shared_path: Optional[Path] = None
    binary: Optional[str] = None
    reason: str = ""
    detail: str = ""
    messages: List[str] = field(default_factory=list)


def converge(project_dir: Path, caller: str,
             binary_resolver=None) -> ConvergeResult:
    """Run the convergence algorithm for one project.

    ``caller`` is ``"init"`` or ``"seam"``; the callers differ in
    ``may_create_without_evidence`` (init: True; seam: False), in failure
    rendering, and in nothing inside the planner/applier.
    ``binary_resolver`` is injectable for tests; the default is
    ``ux._resolve_hook_binary``.
    """
    assert caller in ("init", "seam")
    is_seam = caller == "seam"
    project_dir = Path(project_dir)

    # Step 1 — resolution.
    shared_p = shared_settings_path(project_dir)
    local_p = resolve_local_settings_path(project_dir)

    # Step 2 — read both.
    shared_fs = read_settings(shared_p)
    local_fs = read_settings(local_p)

    # Step 3 — evidence from the readable files.
    def _has_evidence(fs: _FileState) -> bool:
        if fs.state not in ("ok", "absent"):
            return False
        cls = _entries_by_class(fs.doc or {})
        return any(cls[ev][k] for ev in GOVERNED_EVENTS
                   for k in ("managed", "ambiguous"))

    readable_evidence = _has_evidence(shared_fs) or _has_evidence(local_fs)

    # Step 4 — UNKNOWN files.
    for fs, p in ((local_fs, local_p), (shared_fs, shared_p)):
        if fs.state in ("unreadable", "malformed"):
            code = UNREADABLE if fs.state == "unreadable" else MALFORMED
            if not is_seam:
                return ConvergeResult(outcome=code, local_path=local_p,
                                      shared_path=shared_p,
                                      reason=f"{p}: {fs.reason}")
            if readable_evidence:
                return ConvergeResult(outcome=code, local_path=local_p,
                                      shared_path=shared_p,
                                      reason=f"{p}: {fs.reason}")
            return ConvergeResult(outcome=NOOP, local_path=local_p,
                                  shared_path=shared_p)

    # Steps 5–7 — evidence gate, then binary. (The planner re-checks the
    # evidence gate; the binary is resolved here because it is I/O.)
    if not readable_evidence and is_seam:
        return ConvergeResult(outcome=NOOP, local_path=local_p,
                              shared_path=shared_p)

    if binary_resolver is None:
        from sentience_governor.cli import ux as _ux
        binary_resolver = _ux._resolve_hook_binary
    binary = binary_resolver()
    if binary is None:
        if is_seam and not readable_evidence:
            return ConvergeResult(outcome=NOOP, local_path=local_p,
                                  shared_path=shared_p)
        return ConvergeResult(outcome=NO_BINARY, local_path=local_p,
                              shared_path=shared_p)
    if not _verify_path(os.path.expanduser(binary)):
        return ConvergeResult(outcome=BINARY_INVALID, local_path=local_p,
                              shared_path=shared_p, binary=binary)

    # Steps 8–12 — the pure planner.
    plan = plan_convergence(
        local_doc=local_fs.doc or {},
        shared_doc=shared_fs.doc or {},
        binary=binary,
        may_create_without_evidence=(not is_seam),
        caller_is_seam=is_seam,
    )
    if plan.outcome != "plan":
        return ConvergeResult(outcome=plan.outcome, local_path=local_p,
                              shared_path=shared_p, binary=binary,
                              detail=plan.detail)

    # Step 13 — apply.
    status, reason = apply_plan(local_p, local_fs.raw, plan.new_local)
    return ConvergeResult(outcome=status, local_path=local_p,
                          shared_path=shared_p, binary=binary, reason=reason)


# ---------------------------------------------------------------------------
# The on-use seam (§8 — fail-open, never silent failure)
# ---------------------------------------------------------------------------

def run_seam_convergence(cwd: Optional[Path] = None) -> None:
    """Converge the current directory's project from the on-use seam.

    Fail-open: the invoked command always runs — every error is caught
    (``Exception`` only) and at most one stderr line is emitted. Warnings are
    TTY-gated (§9); the file-creation line is NEVER suppressed (§3.4).
    """
    try:
        res = converge(cwd or Path.cwd(), caller="seam")
        _emit_seam_output(res)
    except Exception as exc:
        _warn(f"Sentience: hook-configuration check failed ({exc}); "
              "the requested command is unaffected.")


def _emit_seam_output(res: ConvergeResult) -> None:
    if res.outcome == NOOP or res.outcome == UPDATED:
        return  # success is silent; converging an existing file is silent
    if res.outcome == CREATED:
        # Never TTY-suppressed: a new git-visible file must be attributed.
        print(
            f"Sentience: created {res.local_path} "
            "(machine-local; not for commit)",
            file=sys.stderr,
        )
        return
    if res.outcome == AMBIGUOUS_LOCAL:
        _warn(
            f"Sentience: {res.local_path} contains a modified Sentience hook "
            f"entry ({res.detail}); not changed automatically. Review it, "
            "then run: sentience init claude-code"
        )
    elif res.outcome == SHARED_CONFLICT:
        _warn(
            f"Sentience: {res.shared_path} contains a live Sentience hook "
            "that differs from this install (or one Sentience cannot parse); "
            "capture configuration was not changed. Run: "
            "sentience init claude-code"
        )
    elif res.outcome in (UNREADABLE, MALFORMED):
        _warn(
            f"Sentience: could not read hook configuration ({res.reason}); "
            "capture configuration was not updated."
        )
    elif res.outcome == NO_BINARY:
        _warn(
            "Sentience: cannot locate its own Claude Code hook binary for "
            "this install; this project's capture configuration was not "
            "updated."
        )
    elif res.outcome == BINARY_INVALID:
        _warn(
            f"Sentience: the hook binary for this install ({res.binary}) is "
            "not executable; capture configuration was not updated."
        )
    elif res.outcome in (UNWRITABLE, WRITE_CONFLICT):
        _warn(
            f"Sentience: could not update {res.local_path} ({res.reason}); "
            "capture configuration may be out of date."
        )


def _warn(line: str) -> None:
    """One warning line on stderr, TTY-gated (§9)."""
    if _stderr_isatty():
        print(line, file=sys.stderr)
