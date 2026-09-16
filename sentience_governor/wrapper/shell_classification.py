"""Deterministic, pure, fail-open semantic classifier for shell command text.

v0.3.2. Turns the text of a Bash call into an :class:`OperationClassification`
(``schema/events.py``): ordered segments, each with zero, one or many
``(domain, action, destructive)`` effects. This module

* never executes anything, never touches the filesystem, network or any
  external state, never calls a model, and never interprets a shell program
  recursively;
* never raises for any input (a defect inside the classifier degrades to an
  explicit ``unknown`` effect, not an exception);
* is a bounded, quote-aware scanner plus rule tables. It is deliberately
  **not** a shell parser: what it cannot recognise it marks ``unknown``.

Semantic contract (locked plan §5-§10 with Amendments 1 and 2; the accepted
CP4-D fixture corpus is the oracle):

* **Effects are asserted, never aggregated.** Every triple corresponds to one
  effect the rules established for one segment from deterministic syntax.
  There is no top-level domain or action, and nothing combines fields across
  effects or segments.
* **Actions are conservative** about what the syntax makes possible.
  ``destructive`` is ``True`` only with strong syntactic evidence of
  discarding or state-replacing behaviour, ``False`` when the rules can
  establish non-destructive behaviour, and ``None`` when it depends on
  unseen state, an unseen plan, or an opaque execution.
* **Domains are material governance effects, not transport.** ``network`` is
  emitted when network interaction is the command's purpose (acquisition,
  publication, remote execution, transfer, version-control transport,
  package acquisition), never merely because a control-plane call uses a
  network protocol.
* **Unknown means unknown.** Unknown executables, opaque forms, untabled
  subcommands, unrecognised redirection operators and unsupported nested
  constructs each leave an explicit ``unknown/unknown/null`` effect, and
  ``complete`` is ``False`` iff any effect is ``unknown``. No token,
  substring or hyphen-word inference is performed.
* **Neutral commands** (``cd``, ``echo``, ``export`` ...) produce no segment
  unless they carry a material redirection or an unsupported construct.
* **Transparent wrappers** (``sudo env time nohup nice command exec``) are
  stripped when bare, and ``env``'s ``VAR=value`` arguments with them. A
  wrapper followed by option syntax (``sudo -u root make``) is not parsed:
  its operand structure is not modelled, so the segment is an explicit
  unknown named for the wrapper and no operand is ever reported as the
  executable.
* **Redirections** are material filesystem effects on the segment they
  attach to; descriptor duplication, heredocs and herestrings are not.
  **Amendment 2:** the literal output target ``/dev/null`` is a sink and
  emits no filesystem effect, for recognised shell output redirections and
  for the recognised named-output forms of ``curl`` and ``wget`` only.

Effect order within a segment is fixed: the executable's tabled effects (or
its unknown), then redirection effects in operator order, then at most one
unknown for an unsupported construct (not added when the executable already
contributed an unknown).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sentience_governor.schema.events import (
    ClassifiedEffect,
    ClassifiedSegment,
    OperationClassification,
)

CLASSIFIER_NAME = "shell_rules"
CLASSIFIER_VERSION = 1

Effect = Tuple[str, str, Optional[bool]]
UNKNOWN: Effect = ("unknown", "unknown", None)
DEV_NULL = "/dev/null"

# ---------------------------------------------------------------------------
# Vocabulary tables
# ---------------------------------------------------------------------------

NEUTRAL_COMMANDS: Set[str] = {
    "cd", "pushd", "popd", "export", "set", "unset", "echo", "printf",
    "true", "false", ":", "read", "sleep", "alias",
}
TRANSPARENT_WRAPPERS: Set[str] = {"sudo", "env", "time", "nohup", "nice", "command", "exec"}

FS_READ: Set[str] = {
    "ls", "cat", "head", "tail", "wc", "find", "grep", "rg", "stat", "file", "diff",
    "sort", "uniq", "cut", "tr", "awk", "du", "df", "pwd", "which", "realpath", "tree",
}
FS_CREATE: Set[str] = {"mkdir", "ln"}
FS_MODIFY_TRUE: Set[str] = {"mv", "truncate"}
FS_DELETE: Set[str] = {"rm", "rmdir", "shred"}
FS_OTHER: Set[str] = {"touch", "cp", "tar", "unzip", "gunzip", "gzip", "zip", "chmod", "chown", "tee", "sed"}

VCS_EXECUTABLES: Set[str] = {"git", "hg", "svn"}
NETWORK_EXECUTABLES: Set[str] = {
    "curl", "wget", "scp", "sftp", "rsync", "ssh", "nc", "ncat", "telnet",
    "ping", "dig", "nslookup", "host", "traceroute",
}
NETWORK_READ_TOOLS: Set[str] = {"ping", "dig", "nslookup", "host", "traceroute"}
NETWORK_EXEC_TOOLS: Set[str] = {"ssh", "nc", "ncat", "telnet"}
CLOUD_EXECUTABLES: Set[str] = {"aws", "gcloud", "az", "terraform", "tofu", "kubectl", "helm", "pulumi"}
PACKAGE_EXECUTABLES: Set[str] = {
    "pip", "pipx", "uv", "poetry", "npm", "pnpm", "yarn", "brew", "apt", "apt-get",
    "cargo", "gem", "go", "conda",
}
INTERPRETERS: Set[str] = {"python", "node", "ruby", "perl", "java"}
RUNNERS: Set[str] = {"make", "pytest", "tox"}
OPAQUE_EXECUTABLES: Set[str] = {"sh", "bash", "zsh", "eval", "xargs", "source", "."}
UNKNOWN_BY_DECISION: Set[str] = {"docker", "podman"}

_PYTHON_RE = re.compile(r"^python(\d+(\.\d+)?)?$")
_PIP_RE = re.compile(r"^pip(\d+(\.\d+)?)?$")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z0-9_]+)\1")
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_REMOTE_RE = re.compile(r"^([A-Za-z0-9_.-]+@)?[A-Za-z0-9_.-]+:")
_S3_RE = re.compile(r"^s3://")

# aws verb rules: leading word of the hyphenated verb → effect; exact verbs win.
_AWS_EXACT: Dict[str, Effect] = {
    "invoke": ("cloud_infrastructure", "execute", None),
    "send-command": ("cloud_infrastructure", "execute", None),
    "start-session": ("cloud_infrastructure", "execute", None),
}
_AWS_PREFIX: Dict[str, Effect] = {}
for _w in ("describe", "list", "get", "lookup", "search"):
    _AWS_PREFIX[_w] = ("cloud_infrastructure", "read", False)
for _w in ("create", "run", "launch", "allocate", "register", "import", "copy"):
    _AWS_PREFIX[_w] = ("cloud_infrastructure", "create", False)
_AWS_PREFIX["put"] = ("cloud_infrastructure", "modify", None)
for _w in ("update", "modify", "start", "stop", "reboot", "associate", "attach", "detach",
           "disassociate", "enable", "disable", "set", "tag", "untag"):
    _AWS_PREFIX[_w] = ("cloud_infrastructure", "modify", False)
for _w in ("reset", "restore"):
    _AWS_PREFIX[_w] = ("cloud_infrastructure", "modify", True)
for _w in ("delete", "terminate", "deregister", "release", "remove", "cancel", "purge"):
    _AWS_PREFIX[_w] = ("cloud_infrastructure", "delete", True)
_AWS_PREFIX["execute"] = ("cloud_infrastructure", "execute", None)
_AWS_GLOBAL_FLAG_ONLY: Set[str] = {
    "--debug", "--no-verify-ssl", "--no-paginate", "--no-cli-pager",
    "--no-sign-request", "--no-cli-auto-prompt", "--version",
}

# gcloud / az bounded verb vocabulary (leading word of the token)
_GCLOUD_AZ_VERBS: Dict[str, Effect] = {}
for _w in ("list", "describe", "get", "show"):
    _GCLOUD_AZ_VERBS[_w] = ("cloud_infrastructure", "read", False)
for _w in ("create", "deploy", "import"):
    _GCLOUD_AZ_VERBS[_w] = ("cloud_infrastructure", "create", False)
for _w in ("update", "set", "start", "stop", "restart", "resize", "scale", "add"):
    _GCLOUD_AZ_VERBS[_w] = ("cloud_infrastructure", "modify", False)
for _w in ("delete", "remove", "purge"):
    _GCLOUD_AZ_VERBS[_w] = ("cloud_infrastructure", "delete", True)
for _w in ("ssh", "run", "run-command"):
    _GCLOUD_AZ_VERBS[_w] = ("cloud_infrastructure", "execute", None)

_KUBECTL_VALUE_OPTS: Set[str] = {"-n", "--namespace", "--context", "--kubeconfig", "--cluster", "--user", "-s", "--server"}
_KUBECTL_READ: Set[str] = {"get", "describe", "logs", "top", "explain", "diff", "api-resources", "version"}
_KUBECTL_CREATE: Set[str] = {"create", "run", "expose"}
_KUBECTL_MODIFY: Set[str] = {"patch", "set", "scale", "label", "annotate", "edit", "rollout", "cordon", "uncordon", "taint", "cp"}
_KUBECTL_EXEC: Set[str] = {"exec", "attach", "port-forward"}

_TF_READ: Set[str] = {"plan", "show", "validate", "output", "providers", "graph"}
_HELM_READ: Set[str] = {"list", "status", "get", "show", "template", "lint", "history", "search"}

_PKG_READ: Dict[str, Set[str]] = {
    "pip": {"list", "show", "freeze", "check"}, "pipx": {"list"}, "uv": {"tree"},
    "npm": {"ls", "audit"}, "pnpm": {"ls"}, "yarn": {"list"},
    "brew": {"list", "info", "outdated"}, "apt": {"list", "show"}, "apt-get": set(),
    "cargo": {"tree"}, "poetry": {"show"}, "gem": set(), "go": set(), "conda": set(),
}
_PKG_INSTALL: Dict[str, Set[str]] = {
    "pip": {"install"}, "pipx": {"install"}, "uv": {"add"}, "poetry": {"add"},
    "npm": {"install", "add", "i"}, "pnpm": {"add", "install"}, "yarn": {"add", "install"},
    "brew": {"install"}, "apt": {"install"}, "apt-get": {"install"}, "cargo": {"install"},
    "gem": {"install"}, "go": {"install"}, "conda": {"install"},
}
_PKG_UPGRADE: Dict[str, Set[str]] = {
    "pipx": {"upgrade"}, "poetry": {"update"}, "npm": {"update"}, "brew": {"upgrade", "update"},
    "apt": {"upgrade", "update"}, "apt-get": {"upgrade", "update"}, "cargo": {"update"},
}
_PKG_SYNC_TRUE: Dict[str, Set[str]] = {"npm": {"ci"}, "uv": {"sync"}}
_PKG_REMOVE: Dict[str, Set[str]] = {
    "pip": {"uninstall"}, "pipx": {"uninstall"}, "uv": {"remove"}, "poetry": {"remove"},
    "npm": {"uninstall", "remove"}, "pnpm": {"remove"}, "yarn": {"remove"},
    "brew": {"uninstall", "remove"}, "apt": {"remove", "purge", "autoremove"},
    "apt-get": {"remove", "purge", "autoremove"}, "cargo": {"uninstall"}, "gem": {"uninstall"},
    "conda": {"remove"},
}
_PKG_RUN: Dict[str, Set[str]] = {
    "npm": {"run", "test", "start"}, "pnpm": {"run", "test"}, "yarn": {"run", "test"},
    "uv": {"run"}, "poetry": {"run"}, "cargo": {"build", "test", "run", "check"},
    "go": {"run", "build", "test"},
}

# Redirection operators, longest first.
_REDIR_OPS: Tuple[str, ...] = ("&>>", "<<<", "<<-", "&>", ">>", ">|", ">&", "<<", "<&", "<>", ">", "<")
_OUTPUT_TRUNCATE_OPS: Set[str] = {">", ">|", "&>", ">&"}
_OUTPUT_APPEND_OPS: Set[str] = {">>", "&>>"}


# ---------------------------------------------------------------------------
# Scanner data structures
# ---------------------------------------------------------------------------

@dataclass
class Word:
    value: str = ""        # quotes removed, escapes resolved
    raw: str = ""          # original text
    construct: bool = False  # unsupported nested construct present (outside single quotes)
    group: bool = False    # the word IS a subshell / brace group


@dataclass
class Redirection:
    op: str
    fd: Optional[str]
    target: Optional[Word]


@dataclass
class Segment:
    raw: str
    words: List[Word] = field(default_factory=list)
    redirections: List[Redirection] = field(default_factory=list)
    heredoc: bool = False
    unrecognised_ops: int = 0

    @property
    def has_construct(self) -> bool:
        if any(w.construct for w in self.words):
            return True
        return any(r.target is not None and r.target.construct for r in self.redirections)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def classify_shell_command(command: Any) -> OperationClassification:
    """Classify ``command`` (the text of a Bash call). Never raises."""
    try:
        if not isinstance(command, str):
            return _unknown_classification("")
        return _classify(command)
    except Exception:  # defect inside the classifier: degrade, never raise
        first = ""
        try:
            first = str(command).split()[0] if str(command).split() else ""
        except Exception:
            first = ""
        return _unknown_classification(first)


def target_system_for(classification: OperationClassification) -> str:
    """Coarse compatibility ``target_system`` for a Bash call (locked §9).

    ``shell/<domain>`` iff the classification is ``complete`` and exactly one
    distinct domain appears across every effect of every segment; otherwise
    ``shell``. Any unknown effect (and therefore any unsupported nesting)
    forces ``shell``; zero effects (a fully neutral command) is ``shell``.
    The classification object stays authoritative; this string is the
    legacy surface consumed by scope hints, the ``tools`` regex composite
    and the task-boundary namespace guard.
    """
    try:
        domains = {e.domain for s in classification.segments for e in s.effects}
        if classification.complete and len(domains) == 1:
            return f"shell/{next(iter(domains))}"
    except Exception:
        pass
    return "shell"


def split_segments(command: str) -> List[str]:
    """Top-level segments of ``command`` in order (raw text, stripped).

    Separators at depth 0 outside quotes: ``&&``, ``||``, ``;``, ``|``,
    ``|&``, a single ``&`` (background) and an unescaped newline. A
    backslash-newline is a continuation. ``>|`` is a redirection, not a
    pipe. Heredoc bodies are removed first. Depth counts ``( )``, ``{ }``
    (brace only at the start of a token), ``$( )``, ``<( )``, ``>( )`` and
    backtick pairs; inside them separators do not split.
    """
    command = _strip_heredoc_bodies(command)
    segs: List[str] = []
    cur: List[str] = []
    i, n = 0, len(command)
    depth = 0
    in_bt = False
    q: Optional[str] = None
    tok_start = True
    while i < n:
        c = command[i]
        if q == "'":
            cur.append(c)
            if c == "'":
                q = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                cur.append(" ")
                i += 2
                continue
            cur.append(c + command[i + 1])
            i += 2
            tok_start = False
            continue
        if q == '"':
            cur.append(c)
            if c == '"':
                q = None
            elif c == "$" and command[i + 1:i + 2] == "(":
                depth += 1
                cur.append("(")
                i += 1
            elif c == "`":
                in_bt = not in_bt
            elif c == ")" and depth > 0:
                depth -= 1
            i += 1
            continue
        if c in "'\"":
            q = c
            cur.append(c)
            i += 1
            tok_start = False
            continue
        if c == "`":
            in_bt = not in_bt
            cur.append(c)
            i += 1
            continue
        if in_bt:
            cur.append(c)
            i += 1
            continue
        if (c == "$" and command[i + 1:i + 2] == "(") or (c in "<>" and command[i + 1:i + 2] == "("):
            depth += 1
            cur.append(c + "(")
            i += 2
            continue
        if c == "(":
            depth += 1
            cur.append(c)
            i += 1
            continue
        if c == "{" and tok_start:
            depth += 1
            cur.append(c)
            i += 1
            continue
        if c in ")}" and depth > 0:
            depth -= 1
            cur.append(c)
            i += 1
            continue
        if depth == 0:
            two = command[i:i + 2]
            if two in ("&&", "||", "|&"):
                segs.append("".join(cur))
                cur = []
                i += 2
                tok_start = True
                continue
            if c == "|" and command[i - 1:i] == ">":
                cur.append(c)
                i += 1
                continue
            if c in ";|\n":
                segs.append("".join(cur))
                cur = []
                i += 1
                tok_start = True
                continue
            if c == "&" and two != "&>" and command[i - 1:i] not in (">", "<"):
                segs.append("".join(cur))
                cur = []
                i += 1
                tok_start = True
                continue
        cur.append(c)
        tok_start = c.isspace() or c in ";|&"
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


# ---------------------------------------------------------------------------
# Scanner internals
# ---------------------------------------------------------------------------

def _strip_heredoc_bodies(command: str) -> str:
    lines = command.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        terms: List[str] = []
        q: Optional[str] = None
        j = 0
        while j < len(line):
            c = line[j]
            if q:
                if c == q:
                    q = None
            elif c == "\\":
                j += 1
            elif c in "'\"":
                q = c
            elif line.startswith("<<", j) and not line.startswith("<<<", j):
                m = _HEREDOC_RE.match(line[j:])
                if m:
                    terms.append(m.group(2))
                    j += m.end()
                    continue
            j += 1
        i += 1
        for t in terms:
            while i < len(lines) and lines[i].lstrip("\t") != t:
                i += 1
            i += 1
    return "\n".join(out)


def _scan_segment(raw: str) -> Segment:
    """Tokenise one raw segment into words and redirections.

    Quote-aware; tracks construct depth so that ``$(a b)`` stays one word;
    detects unsupported constructs outside single quotes; recognises
    redirection operators (spaced and attached) outside quotes.
    """
    seg = Segment(raw=raw)
    i, n = 0, len(raw)
    w = Word()
    started = False
    q: Optional[str] = None
    depth = 0
    in_bt = False

    def flush() -> None:
        nonlocal w, started
        if started:
            seg.words.append(w)
        w = Word()
        started = False

    def read_word(k: int) -> Tuple[Word, int]:
        """Read one quote-aware word starting at k (skipping leading spaces)."""
        while k < n and raw[k].isspace():
            k += 1
        tw = Word()
        tq: Optional[str] = None
        tdepth = 0
        tbt = False
        began = False
        while k < n:
            c = raw[k]
            if tq == "'":
                tw.raw += c
                if c == "'":
                    tq = None
                else:
                    tw.value += c
                k += 1
                continue
            if c == "\\" and k + 1 < n:
                nxt = raw[k + 1]
                tw.raw += c + nxt
                if tq == '"' and nxt not in "$`\"\\\n":
                    tw.value += c
                tw.value += nxt
                k += 2
                began = True
                continue
            if tq == '"':
                tw.raw += c
                if c == '"':
                    tq = None
                elif c == "$" and raw[k + 1:k + 2] == "(":
                    tw.construct = True
                    tdepth += 1
                    tw.raw += "("
                    tw.value += c + "("
                    k += 1
                elif c == "`":
                    tw.construct = True
                    tbt = not tbt
                    tw.value += c
                elif c == ")" and tdepth > 0:
                    tdepth -= 1
                    tw.value += c
                else:
                    tw.value += c
                k += 1
                continue
            if tbt:
                tw.raw += c
                tw.value += c
                if c == "`":
                    tbt = False
                k += 1
                continue
            if tdepth == 0 and (c.isspace() or (began and c in "<>&|;")):
                break
            if c in "'\"":
                tq = c
                tw.raw += c
                began = True
                k += 1
                continue
            if c == "`":
                tw.construct = True
                tbt = True
                tw.raw += c
                tw.value += c
                began = True
                k += 1
                continue
            if (c in "$<>") and raw[k + 1:k + 2] == "(":
                tw.construct = True
                tdepth += 1
                tw.raw += c + "("
                tw.value += c + "("
                began = True
                k += 2
                continue
            if c == "(":
                if not began and tdepth == 0:
                    tw.group = True
                else:
                    tw.construct = True
                tdepth += 1
                tw.raw += c
                tw.value += c
                began = True
                k += 1
                continue
            if c == "{" and not began and (k + 1 >= n or raw[k + 1].isspace()):
                tw.group = True
                tdepth += 1
                tw.raw += c
                tw.value += c
                began = True
                k += 1
                continue
            if c in ")}" and tdepth > 0:
                tdepth -= 1
                tw.raw += c
                tw.value += c
                k += 1
                continue
            tw.raw += c
            tw.value += c
            began = True
            k += 1
        return tw, k

    while i < n:
        c = raw[i]
        if q == "'":
            w.raw += c
            if c == "'":
                q = None
            else:
                w.value += c
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            w.raw += c + nxt
            if q == '"' and nxt not in "$`\"\\\n":
                w.value += c
            w.value += nxt
            started = True
            i += 2
            continue
        if q == '"':
            w.raw += c
            if c == '"':
                q = None
            elif c == "$" and raw[i + 1:i + 2] == "(":
                w.construct = True
                depth += 1
                w.raw += "("
                w.value += c + "("
                i += 1
            elif c == "`":
                w.construct = True
                in_bt = not in_bt
                w.value += c
            elif c == ")" and depth > 0:
                depth -= 1
                w.value += c
            else:
                w.value += c
            i += 1
            continue
        if in_bt:
            w.raw += c
            w.value += c
            if c == "`":
                in_bt = False
            i += 1
            continue
        if depth > 0:
            # inside an unquoted construct/group: keep everything in the word
            w.raw += c
            w.value += c
            if c == "$" and raw[i + 1:i + 2] == "(":
                depth += 1
                w.raw += "("
                w.value += "("
                i += 1
            elif c in "({":
                depth += 1
            elif c in ")}":
                depth -= 1
            elif c == "`":
                in_bt = True
            elif c in "'\"":
                # quotes inside a construct are opaque data; skip to close
                j = raw.find(c, i + 1)
                if j == -1:
                    j = n - 1
                w.raw += raw[i + 1:j + 1]
                w.value += raw[i + 1:j + 1]
                i = j
            i += 1
            continue
        if c.isspace():
            flush()
            i += 1
            continue
        if c in "'\"":
            q = c
            w.raw += c
            started = True
            i += 1
            continue
        if c == "`":
            w.construct = True
            in_bt = True
            w.raw += c
            w.value += c
            started = True
            i += 1
            continue
        # process substitution `<(`/`>(` and `$(` are constructs, not redirections
        if (c in "<>$") and raw[i + 1:i + 2] == "(":
            w.construct = True
            depth += 1
            w.raw += c + "("
            w.value += c + "("
            started = True
            i += 2
            continue
        # redirection operator?
        op = None
        if c in "<>&":
            for cand in _REDIR_OPS:
                if raw.startswith(cand, i):
                    if cand.startswith("&") and raw[i + 1:i + 2] != ">":
                        continue
                    op = cand
                    break
        if op is not None and not (c == "&" and op is None):
            fd: Optional[str] = None
            if started and w.value.isdigit() and w.raw == w.value:
                fd = w.value
                w = Word()
                started = False
            else:
                flush()
            i += len(op)
            if op in ("<<", "<<-"):
                seg.heredoc = True
                # consume the terminator word
                _, i = read_word(i)
                seg.redirections.append(Redirection(op, fd, None))
                continue
            if op == "<<<":
                _, i = read_word(i)
                seg.redirections.append(Redirection(op, fd, None))
                continue
            target, i = read_word(i)
            seg.redirections.append(Redirection(op, fd, target))
            continue
        if c == "(":
            if not started:
                w.group = True
            else:
                w.construct = True
            depth += 1
            w.raw += c
            w.value += c
            started = True
            i += 1
            continue
        if c == "{" and not started and (i + 1 >= n or raw[i + 1].isspace()):
            w.group = True
            depth += 1
            w.raw += c
            w.value += c
            started = True
            i += 1
            continue
        w.raw += c
        w.value += c
        started = True
        i += 1
    flush()
    return seg


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _unknown_classification(executable: str) -> OperationClassification:
    return OperationClassification(
        classifier=CLASSIFIER_NAME,
        classifier_version=CLASSIFIER_VERSION,
        complete=False,
        destructive=None,
        segments=[ClassifiedSegment(executable=executable, effects=[_effect(UNKNOWN)])],
    )


def _effect(e: Effect) -> ClassifiedEffect:
    return ClassifiedEffect(domain=e[0], action=e[1], destructive=e[2])


def _classify(command: str) -> OperationClassification:
    segments: List[ClassifiedSegment] = []
    all_effects: List[Effect] = []
    for raw in split_segments(command):
        result = _classify_segment(raw)
        if result is None:
            continue
        executable, subcommand, effects = result
        all_effects.extend(effects)
        segments.append(
            ClassifiedSegment(
                executable=executable,
                subcommand=subcommand,
                effects=[_effect(e) for e in effects],
            )
        )
    complete = all(e[0] != "unknown" for e in all_effects)
    if any(e[2] is True for e in all_effects):
        destructive: Optional[bool] = True
    elif complete and all(e[2] is False for e in all_effects):
        destructive = False
    else:
        destructive = None
    return OperationClassification(
        classifier=CLASSIFIER_NAME,
        classifier_version=CLASSIFIER_VERSION,
        complete=complete,
        destructive=destructive,
        segments=segments,
    )


def _classify_segment(raw: str) -> Optional[Tuple[str, Optional[str], List[Effect]]]:
    """Return ``(executable, subcommand, effects)`` or ``None`` for a neutral segment."""
    seg = _scan_segment(raw)
    words = list(seg.words)

    # A subshell or brace group as the segment: one unknown segment.
    if words and words[0].group:
        effects: List[Effect] = [UNKNOWN]
        effects.extend(_redirection_effects(seg))
        return words[0].value[:1], None, effects

    # Leading assignments and transparent wrappers.
    assign_names: List[str] = []
    assign_construct = False
    idx = 0
    while idx < len(words):
        wv = words[idx]
        m = _ASSIGN_RE.match(wv.raw)
        if m and not wv.group:
            assign_names.append(m.group(0))
            assign_construct = assign_construct or wv.construct
            idx += 1
            continue
        if wv.value in TRANSPARENT_WRAPPERS and not wv.construct:
            if wv.value == "exec" and idx + 1 >= len(words):
                break  # bare exec: not a wrapper use
            idx += 1
            if idx < len(words) and words[idx].value.startswith("-"):
                # Wrapper option syntax (`sudo -u root make`, `env -u FOO make`,
                # `nice -n 10 make`, `time -p make`): the operand structure is
                # not modelled in v0.3.2, so the classifier must not skip
                # tokens and guess which word is the real executable. The
                # segment is an explicit unknown named for the wrapper; no
                # option operand is ever reported as the executable.
                return wv.value, None, [UNKNOWN] + _redirection_effects(seg)
            continue
        break
    rest = words[idx:]

    if not rest:
        # assignment-only (or wrapper-only) segment
        if assign_construct:
            return assign_names[-1], None, [UNKNOWN] + _redirection_effects(seg)
        if seg.redirections or seg.unrecognised_ops:
            red = _redirection_effects(seg)
            if red:
                name = assign_names[-1] if assign_names else (words[-1].value if words else "")
                return name, None, red
        return None

    exe_word = rest[0]
    args = rest[1:]
    executable, family = _normalize_executable(exe_word)
    construct = seg.has_construct or assign_construct

    if family == "neutral":
        red = _redirection_effects(seg)
        if not red and not construct:
            return None
        effects = list(red)
        if construct:
            effects.append(UNKNOWN)
        return executable, None, effects

    subcommand, effects = _table_effects(executable, family, args, seg)
    exe_unknown = any(e[0] == "unknown" for e in effects)
    effects = effects + _redirection_effects(seg)
    if construct and not exe_unknown:
        effects.append(UNKNOWN)
    return executable, subcommand, effects


def _normalize_executable(word: Word) -> Tuple[str, str]:
    """``(executable, family)``. Known executables are reduced to their basename."""
    value = word.value
    base = value.rsplit("/", 1)[-1] if "/" in value else value
    if word.construct or word.group:
        return value, "unknown"
    if base in NEUTRAL_COMMANDS and base == value:
        return value, "neutral"
    if _PYTHON_RE.match(base):
        return base, "python"
    if _PIP_RE.match(base):
        return base, "pip"
    if base in VCS_EXECUTABLES:
        return base, "vcs"
    if base == "gh":
        return base, "gh"
    if base in FS_READ or base in FS_CREATE or base in FS_MODIFY_TRUE or base in FS_DELETE or base in FS_OTHER:
        return base, "fs"
    if base in PACKAGE_EXECUTABLES:
        return base, "pkg"
    if base in NETWORK_EXECUTABLES:
        return base, "net"
    if base in CLOUD_EXECUTABLES:
        return base, "cloud"
    if base in INTERPRETERS or base in RUNNERS:
        return base, "process"
    if base in OPAQUE_EXECUTABLES:
        return base, "opaque"
    if base in UNKNOWN_BY_DECISION:
        return base, "decided_unknown"
    return value, "unknown"


# ---------------------------------------------------------------------------
# Redirections
# ---------------------------------------------------------------------------

def _redirection_effects(seg: Segment) -> List[Effect]:
    out: List[Effect] = []
    for r in seg.redirections:
        if r.op in ("<<", "<<-", "<<<"):
            continue
        target = r.target
        tval = target.value if target is not None else ""
        if r.op in ("<&", ">&") and (tval.isdigit() or tval == "-"):
            if tval == "-":
                out.append(UNKNOWN)  # closing a descriptor: unrecognised form
            continue  # descriptor duplication: no effect
        if r.op == "<>":
            out.append(UNKNOWN)
            if target is not None and target.construct:
                pass
            continue
        if target is None or not tval:
            out.append(UNKNOWN)
            continue
        if r.op in _OUTPUT_TRUNCATE_OPS:
            if tval != DEV_NULL:
                out.append(("filesystem", "modify", None))
        elif r.op in _OUTPUT_APPEND_OPS:
            if tval != DEV_NULL:
                out.append(("filesystem", "modify", False))
        elif r.op == "<":
            out.append(("filesystem", "read", False))
        else:
            out.append(UNKNOWN)
    return out


# ---------------------------------------------------------------------------
# Family tables
# ---------------------------------------------------------------------------

def _positionals(args: Sequence[Word]) -> List[str]:
    return [a.value for a in args if not a.value.startswith("-")]


def _flags(args: Sequence[Word]) -> List[str]:
    return [a.value for a in args if a.value.startswith("-")]


def _table_effects(executable: str, family: str, args: List[Word], seg: Segment) -> Tuple[Optional[str], List[Effect]]:
    if family in ("unknown", "decided_unknown", "opaque"):
        sub = None
        if family == "decided_unknown" and args:
            sub = args[0].value if not args[0].value.startswith("-") else None
        return sub, [UNKNOWN]
    if family == "fs":
        return None, _fs_effects(executable, args)
    if family == "vcs":
        return _vcs_effects(executable, args)
    if family == "gh":
        return _gh_effects(args)
    if family in ("pkg", "pip"):
        return _pkg_effects(executable, family, args)
    if family == "net":
        return None, _net_effects(executable, args)
    if family == "cloud":
        return _cloud_effects(executable, args)
    if family in ("python", "process"):
        return _process_effects(executable, family, args, seg)
    return None, [UNKNOWN]


def _fs_effects(exe: str, args: List[Word]) -> List[Effect]:
    flags = _flags(args)
    if exe in FS_READ:
        return [("filesystem", "read", False)]
    if exe in FS_CREATE:
        return [("filesystem", "create", False)]
    if exe in FS_MODIFY_TRUE:
        return [("filesystem", "modify", True)]
    if exe in FS_DELETE:
        return [("filesystem", "delete", True)]
    if exe == "touch":
        return [("filesystem", "modify", False)]
    if exe == "cp":
        return [("filesystem", "modify", None)]
    if exe == "tar":
        first = args[0].value if args else ""
        letters = first.lstrip("-") if not first.startswith("--") else ""
        long_flags = set(flags)
        if "x" in letters or "--extract" in long_flags or "c" in letters or "--create" in long_flags:
            return [("filesystem", "modify", None)]
        return [UNKNOWN]
    if exe in ("unzip", "gunzip", "zip"):
        return [("filesystem", "modify", None)]
    if exe == "gzip":
        if any(f in ("-d", "--decompress", "-k", "--keep") or (f.startswith("-") and not f.startswith("--") and ("d" in f or "k" in f)) for f in flags):
            return [("filesystem", "modify", None)]
        return [("filesystem", "modify", True)]
    if exe in ("chmod", "chown"):
        return [("filesystem", "modify", False)]
    if exe == "tee":
        plain = [a for a in args if not a.value.startswith("-") and not a.construct and not a.group]
        if not plain:
            return []
        if any(f in ("-a", "--append") or (f.startswith("-") and not f.startswith("--") and "a" in f) for f in flags):
            return [("filesystem", "modify", False)]
        return [("filesystem", "modify", None)]
    if exe == "sed":
        if any(f == "--in-place" or f.startswith("--in-place=") or (f.startswith("-i") and not f.startswith("--")) for f in flags):
            return [("filesystem", "modify", True)]
        return [("filesystem", "read", False)]
    return [UNKNOWN]


_GIT_VALUE_OPTS: Set[str] = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
_GIT_READ: Set[str] = {"status", "log", "diff", "show", "blame", "ls-files", "rev-parse", "describe"}
_GIT_CREATE: Set[str] = {"commit", "init"}
_GIT_MODIFY: Set[str] = {"merge", "cherry-pick", "revert", "am", "apply", "mv"}
_GIT_MODIFY_TRUE: Set[str] = {"restore", "rebase"}
_GIT_DELETE: Set[str] = {"rm", "clean"}


def _vcs_effects(exe: str, args: List[Word]) -> Tuple[Optional[str], List[Effect]]:
    i = 0
    while i < len(args) and args[i].value.startswith("-"):
        if args[i].value in _GIT_VALUE_OPTS:
            i += 2
        else:
            i += 1
    if i >= len(args):
        return None, [UNKNOWN]
    sub = args[i].value
    rest = args[i + 1:]
    flags = _flags(rest)
    pos = _positionals(rest)
    vc = "version_control"
    if sub in _GIT_READ:
        return sub, [(vc, "read", False)]
    if sub == "remote":
        if not rest or all(f in ("-v", "--verbose") for f in flags) and not pos:
            return sub, [(vc, "read", False)]
        return sub, [UNKNOWN]
    if sub == "branch":
        if any(f in ("-d", "-D", "--delete") for f in flags):
            return sub, [(vc, "delete", True)]
        if pos:
            return sub, [(vc, "create", False)]
        return sub, [(vc, "read", False)]
    if sub == "tag":
        if any(f in ("-d", "--delete") for f in flags):
            return sub, [(vc, "delete", True)]
        if pos:
            return sub, [(vc, "create", False)]
        return sub, [(vc, "read", False)]
    if sub == "stash":
        verb = pos[0] if pos else "push"
        if verb == "list":
            return sub, [(vc, "read", False)]
        if verb in ("pop", "apply"):
            return sub, [(vc, "modify", False)]
        if verb in ("drop", "clear"):
            return sub, [(vc, "delete", True)]
        if verb == "push":
            return sub, [(vc, "create", False)]
        return sub, [UNKNOWN]
    if sub == "config":
        if any(f.startswith("--get") or f in ("-l", "--list") for f in flags):
            return sub, [(vc, "read", False)]
        return sub, [(vc, "modify", False)]
    if sub == "clone":
        if any(_URL_RE.match(p) or _REMOTE_RE.match(p) for p in pos):
            return sub, [("network", "read", False), (vc, "create", False)]
        return sub, [(vc, "create", False)]
    if sub == "fetch":
        return sub, [("network", "read", False), (vc, "modify", False)]
    if sub == "pull":
        if any(f in ("--rebase", "-r") or f.startswith("--rebase=") for f in flags):
            return sub, [("network", "read", False), (vc, "modify", True)]
        return sub, [("network", "read", False), (vc, "modify", False)]
    if sub == "push":
        if any(f in ("--delete", "-d") for f in flags) or any(p.startswith(":") for p in pos):
            return sub, [("network", "modify", False), (vc, "delete", True)]
        if any(f in ("--force", "-f", "--force-with-lease") or f.startswith("--force-with-lease=") for f in flags):
            return sub, [("network", "modify", False), (vc, "modify", True)]
        return sub, [("network", "modify", False), (vc, "modify", False)]
    if sub in _GIT_CREATE:
        return sub, [(vc, "create", False)]
    if sub == "add":
        return sub, [(vc, "modify", False)]
    if sub == "checkout":
        if any(f in ("-b", "-B", "--orphan") for f in flags):
            return sub, [(vc, "create", False)]
        if any(f in ("-f", "--force") for f in flags) or any(a.value == "--" for a in rest):
            return sub, [(vc, "modify", True)]
        return sub, [(vc, "modify", False)]
    if sub == "switch":
        if any(f in ("-c", "-C", "--create", "--force-create") for f in flags):
            return sub, [(vc, "create", False)]
        if any(f in ("-f", "--force", "--discard-changes") for f in flags):
            return sub, [(vc, "modify", True)]
        return sub, [(vc, "modify", False)]
    if sub == "reset":
        if "--hard" in flags:
            return sub, [(vc, "modify", True)]
        return sub, [(vc, "modify", False)]
    if sub == "worktree":
        verb = pos[0] if pos else ""
        if verb == "add":
            return sub, [(vc, "create", False)]
        if verb == "remove":
            return sub, [(vc, "delete", True)]
        return sub, [UNKNOWN]
    if sub in _GIT_MODIFY:
        return sub, [(vc, "modify", False)]
    if sub in _GIT_MODIFY_TRUE:
        return sub, [(vc, "modify", True)]
    if sub in _GIT_DELETE:
        return sub, [(vc, "delete", True)]
    return sub, [UNKNOWN]


_GH_GROUPS: Set[str] = {"pr", "repo", "run", "release", "auth", "issue", "gist", "workflow", "secret", "label"}


def _gh_effects(args: List[Word]) -> Tuple[Optional[str], List[Effect]]:
    pos = _positionals(args)
    if not pos:
        return None, [UNKNOWN]
    if pos[0] not in _GH_GROUPS or len(pos) < 2:
        return pos[0], [UNKNOWN]
    sub = f"{pos[0]} {pos[1]}"
    nw_read: List[Effect] = [("network", "read", False)]
    if sub in ("pr list", "pr view", "pr status", "repo view", "run list", "run view"):
        return sub, nw_read
    if sub in ("pr create", "repo create", "release create"):
        return sub, [("network", "modify", False), ("version_control", "create", False)]
    if sub in ("pr merge", "pr edit", "pr review", "pr comment", "pr close"):
        return sub, [("network", "modify", False), ("version_control", "modify", False)]
    if sub in ("repo delete", "release delete"):
        return sub, [("network", "modify", False), ("version_control", "delete", True)]
    return sub, [UNKNOWN]


def _is_local_path(target: str) -> bool:
    if target in (".", ".."):
        return True
    if target.startswith(("./", "../", "/", "~")):
        return True
    return target.endswith((".whl", ".tar.gz", ".zip"))


def _pkg_effects(exe: str, family: str, args: List[Word]) -> Tuple[Optional[str], List[Effect]]:
    mgr = "pip" if family == "pip" else exe
    pos = _positionals(args)
    flags = _flags(args)
    if not pos:
        return None, [UNKNOWN]
    sub = pos[0]
    rest_pos = pos[1:]
    if mgr == "uv" and sub == "pip" and len(pos) >= 2:
        sub = f"pip {pos[1]}"
        rest_pos = pos[2:]
        if pos[1] == "list":
            return sub, [("packages", "read", False)]
        if pos[1] == "install":
            return sub, _install_effects("pip", rest_pos, args)
        return sub, [UNKNOWN]
    if mgr == "uv" and sub == "lock":
        if "--upgrade" in flags:
            return sub, [("network", "read", False), ("packages", "modify", False)]
        return sub, [UNKNOWN]
    if sub in _PKG_RUN.get(mgr, set()):
        return sub, [("process", "execute", None)]
    if sub in _PKG_READ.get(mgr, set()):
        eff: List[Effect] = [("packages", "read", False)]
        if mgr == "npm" and sub in ("view", "outdated"):
            eff.append(("network", "read", False))
        return sub, eff
    if mgr == "npm" and sub in ("view", "outdated"):
        return sub, [("packages", "read", False), ("network", "read", False)]
    if mgr == "pip" and sub == "download":
        return sub, [("network", "read", False), ("filesystem", "modify", None)]
    if sub in _PKG_INSTALL.get(mgr, set()):
        if mgr == "poetry" and sub == "install":
            return sub, [UNKNOWN]
        return sub, _install_effects(mgr, rest_pos, args)
    if mgr == "poetry" and sub == "install":
        if "--sync" in flags:
            return sub, [("network", "read", False), ("packages", "modify", True)]
        return sub, [UNKNOWN]
    if sub in _PKG_UPGRADE.get(mgr, set()):
        return sub, [("network", "read", False), ("packages", "modify", False)]
    if sub in _PKG_SYNC_TRUE.get(mgr, set()):
        return sub, [("network", "read", False), ("packages", "modify", True)]
    if sub in _PKG_REMOVE.get(mgr, set()):
        return sub, [("packages", "delete", True)]
    return sub, [UNKNOWN]


def _install_effects(mgr: str, targets: List[str], args: List[Word]) -> List[Effect]:
    if mgr in ("pip", "pipx", "uv"):
        # -e VALUE counts as a target; -r FILE does not
        editable: List[str] = []
        vals = [a.value for a in args]
        for j, v in enumerate(vals):
            if v in ("-e", "--editable") and j + 1 < len(vals):
                editable.append(vals[j + 1])
        all_targets = [t for t in targets if t not in editable] + editable
        # values consumed by -r/-c/-e are not positional targets
        consumed = set()
        for j, v in enumerate(vals):
            if v in ("-r", "--requirement", "-c", "--constraint", "-e", "--editable") and j + 1 < len(vals):
                consumed.add(vals[j + 1])
        all_targets = [t for t in all_targets if t not in consumed] + editable
        # A requirements file names index targets (PK-24 reasoning): it
        # contributes a non-local target, so `-e . -r dev.txt` still acquires.
        if any(v in ("-r", "--requirement") or v.startswith("--requirement=") for v in vals):
            all_targets.append("<requirements>")
        if all_targets and all(_is_local_path(t) for t in all_targets):
            return [("packages", "modify", False)]
    return [("network", "read", False), ("packages", "modify", False)]


def _net_effects(exe: str, args: List[Word]) -> List[Effect]:
    if exe in NETWORK_READ_TOOLS:
        return [("network", "read", False)]
    if exe in NETWORK_EXEC_TOOLS:
        return [("network", "execute", None)]
    if exe == "curl":
        return _curl_effects(args)
    if exe == "wget":
        return _wget_effects(args)
    if exe == "sftp":
        return [("network", "modify", None)]
    if exe in ("scp", "rsync"):
        return _copy_effects(exe, args)
    return [UNKNOWN]


def _curl_effects(args: List[Word]) -> List[Effect]:
    vals = [a.value for a in args]
    method: Optional[str] = None
    data = False
    output: Optional[str] = None
    head = False
    j = 0
    while j < len(vals):
        v = vals[j]
        nxt = vals[j + 1] if j + 1 < len(vals) else ""
        if v in ("-X", "--request"):
            method = nxt.upper()
            j += 2
            continue
        if v.startswith("--request="):
            method = v.split("=", 1)[1].upper()
        elif v.startswith("-X") and len(v) > 2 and not v.startswith("--"):
            method = v[2:].upper()
        elif v in ("-o", "--output"):
            output = nxt
            j += 2
            continue
        elif v.startswith("--output="):
            output = v.split("=", 1)[1]
        elif v in ("-O", "--remote-name"):
            output = "<remote-name>"
        elif v in ("-d", "--data", "-T", "--upload-file") or v.startswith("--data-") or v.startswith("--data="):
            data = True
            if v in ("-d", "--data", "-T", "--upload-file"):
                j += 2
                continue
        elif v in ("-I", "--head"):
            head = True
        elif v.startswith("-") and not v.startswith("--") and len(v) > 2:
            # single-dash cluster: named letters o / O / d / T / I / X
            cluster = v[1:]
            if "X" in cluster:
                method = (cluster.split("X", 1)[1] or nxt).upper()
                if not cluster.split("X", 1)[1]:
                    j += 2
                    continue
            if "o" in cluster:
                output = nxt
                j += 2
                continue
            if "O" in cluster:
                output = "<remote-name>"
            if "d" in cluster or "T" in cluster:
                data = True
                j += 2
                continue
            if "I" in cluster:
                head = True
        j += 1
    effects: List[Effect]
    if method is not None:
        if method in ("GET", "HEAD", "OPTIONS"):
            effects = [("network", "read", False)]
        elif method == "DELETE":
            effects = [("network", "delete", True)]
        else:
            effects = [("network", "modify", None)]
    elif data:
        effects = [("network", "modify", None)]
    else:
        effects = [("network", "read", False)]
    if output is not None and output != DEV_NULL:
        effects.append(("filesystem", "modify", None))
    return effects


def _wget_effects(args: List[Word]) -> List[Effect]:
    vals = [a.value for a in args]
    output: Optional[str] = None
    post = False
    j = 0
    while j < len(vals):
        v = vals[j]
        if v in ("-O", "--output-document"):
            output = vals[j + 1] if j + 1 < len(vals) else ""
            j += 2
            continue
        if v.startswith("--output-document="):
            output = v.split("=", 1)[1]
        elif v.startswith("--post-"):
            post = True
        j += 1
    if post:
        return [("network", "modify", None)]
    effects: List[Effect] = [("network", "read", False)]
    if output == "-":
        return effects
    if output is None or output != DEV_NULL:
        effects.append(("filesystem", "modify", None))
    return effects


def _copy_effects(exe: str, args: List[Word]) -> List[Effect]:
    pos = [a.value for a in args if not a.value.startswith("-")]
    delete = exe == "rsync" and any(a.value == "--delete" or a.value.startswith("--delete-") for a in args)
    if len(pos) < 2:
        return [UNKNOWN]
    src, dst = pos[:-1], pos[-1]

    def remote(p: str) -> bool:
        return bool(_URL_RE.match(p) or _REMOTE_RE.match(p))

    if remote(dst):
        return [("network", "modify", True if delete else None)]
    if any(remote(s) for s in src):
        return [("network", "read", False), ("filesystem", "modify", True if delete else None)]
    return [("filesystem", "modify", True if delete else None)]


def _cloud_effects(exe: str, args: List[Word]) -> Tuple[Optional[str], List[Effect]]:
    vals = [a.value for a in args]
    ci = "cloud_infrastructure"
    if exe == "aws":
        j = 0
        while j < len(vals) and vals[j].startswith("-"):
            if vals[j] in _AWS_GLOBAL_FLAG_ONLY or "=" in vals[j]:
                j += 1
            else:
                j += 2
        if j + 1 >= len(vals):
            return (vals[j] if j < len(vals) else None), [UNKNOWN]
        service, verb = vals[j], vals[j + 1]
        sub = f"{service} {verb}"
        rest = [v for v in vals[j + 2:]]
        if service == "s3" and verb in ("cp", "sync"):
            pos = [v for v in rest if not v.startswith("-")]
            if pos and _S3_RE.match(pos[-1]):
                return sub, [(ci, "modify", None)]
            return sub, [UNKNOWN]
        if verb in _AWS_EXACT:
            return sub, [_AWS_EXACT[verb]]
        lead = verb.split("-", 1)[0]
        if "-" in verb and lead in _AWS_PREFIX:
            return sub, [_AWS_PREFIX[lead]]
        return sub, [UNKNOWN]
    if exe in ("gcloud", "az"):
        seen_verb = False
        verb_idx = -1
        for j, v in enumerate(vals):
            if v.startswith("-"):
                break
            lead = v.split("-", 1)[0]
            key = v if v in _GCLOUD_AZ_VERBS else lead
            if key in _GCLOUD_AZ_VERBS and (v == key or v.startswith(key + "-")):
                seen_verb = True
                verb_idx = j
            elif seen_verb:
                break
        if verb_idx < 0:
            return (" ".join(v for v in vals if not v.startswith("-")) or None), [UNKNOWN]
        verb = vals[verb_idx]
        key = verb if verb in _GCLOUD_AZ_VERBS else verb.split("-", 1)[0]
        return " ".join(vals[: verb_idx + 1]), [_GCLOUD_AZ_VERBS[key]]
    if exe == "kubectl":
        j = 0
        while j < len(vals) and vals[j].startswith("-"):
            j += 2 if vals[j] in _KUBECTL_VALUE_OPTS else 1
        if j >= len(vals):
            return None, [UNKNOWN]
        verb = vals[j]
        rest = vals[j + 1:]
        flags = [v for v in rest if v.startswith("-")]
        if verb in ("config", "auth", "rollout"):
            second = rest[0] if rest and not rest[0].startswith("-") else ""
            sub = f"{verb} {second}".strip()
            if verb == "config" and second == "view":
                return sub, [(ci, "read", False)]
            if verb == "auth" and second == "can-i":
                return sub, [(ci, "read", False)]
            if verb == "rollout":
                return sub, [(ci, "modify", False)]
            return sub, [UNKNOWN]
        if verb in _KUBECTL_READ:
            return verb, [(ci, "read", False)]
        if verb in _KUBECTL_CREATE:
            return verb, [(ci, "create", False)]
        if verb == "apply":
            return verb, [(ci, "modify", True if any(f in ("--prune", "--force") for f in flags) else None)]
        if verb == "replace":
            return verb, [(ci, "modify", True if "--force" in flags else None)]
        if verb in _KUBECTL_MODIFY:
            return verb, [(ci, "modify", False)]
        if verb == "drain":
            return verb, [(ci, "modify", True)]
        if verb == "delete":
            return verb, [(ci, "delete", True)]
        if verb in _KUBECTL_EXEC:
            return verb, [(ci, "execute", None)]
        return verb, [UNKNOWN]
    if exe in ("terraform", "tofu"):
        pos = [v for v in vals if not v.startswith("-")]
        flags = [v for v in vals if v.startswith("-")]
        if not pos:
            return None, [UNKNOWN]
        verb = pos[0]
        if verb in ("state", "workspace"):
            second = pos[1] if len(pos) > 1 else ""
            sub = f"{verb} {second}".strip()
            if verb == "state":
                if second in ("list", "show"):
                    return sub, [(ci, "read", False)]
                if second == "mv":
                    return sub, [(ci, "modify", True)]
                if second == "rm":
                    return sub, [(ci, "delete", True)]
            else:
                if second in ("list", "show"):
                    return sub, [(ci, "read", False)]
                if second == "select":
                    return sub, [(ci, "modify", False)]
                if second == "new":
                    return sub, [(ci, "create", False)]
            return sub, [UNKNOWN]
        if verb in _TF_READ:
            return verb, [(ci, "read", False)]
        if verb == "init":
            return verb, [("network", "read", False), (ci, "modify", False)]
        if verb == "fmt":
            return verb, [("filesystem", "modify", False)]
        if verb == "apply":
            if "-destroy" in flags or "--destroy" in flags:
                return verb, [(ci, "delete", True)]
            return verb, [(ci, "modify", None)]
        if verb in ("import", "refresh", "untaint"):
            return verb, [(ci, "modify", False)]
        if verb == "taint":
            return verb, [(ci, "modify", True)]
        if verb == "destroy":
            return verb, [(ci, "delete", True)]
        return verb, [UNKNOWN]
    if exe == "helm":
        pos = [v for v in vals if not v.startswith("-")]
        if not pos:
            return None, [UNKNOWN]
        verb = pos[0]
        if verb == "repo":
            second = pos[1] if len(pos) > 1 else ""
            sub = f"repo {second}".strip()
            if second in ("add", "update"):
                return sub, [("network", "read", False), ("packages", "modify", False)]
            return sub, [UNKNOWN]
        if verb in _HELM_READ:
            return verb, [(ci, "read", False)]
        if verb == "install":
            return verb, [(ci, "create", False)]
        if verb == "upgrade":
            return verb, [(ci, "modify", None)]
        if verb == "rollback":
            return verb, [(ci, "modify", True)]
        if verb in ("uninstall", "delete"):
            return verb, [(ci, "delete", True)]
        return verb, [UNKNOWN]
    if exe == "pulumi":
        pos = [v for v in vals if not v.startswith("-")]
        if not pos:
            return None, [UNKNOWN]
        verb = pos[0]
        if verb in ("stack", "config"):
            second = pos[1] if len(pos) > 1 else ""
            sub = f"{verb} {second}".strip()
            if verb == "stack":
                if second in ("ls", "output"):
                    return sub, [(ci, "read", False)]
                if second == "init":
                    return sub, [(ci, "create", False)]
                if second == "rm":
                    return sub, [(ci, "delete", True)]
            else:
                if second == "get":
                    return sub, [(ci, "read", False)]
                if second == "set":
                    return sub, [(ci, "modify", False)]
            return sub, [UNKNOWN]
        if verb == "preview":
            return verb, [(ci, "read", False)]
        if verb == "up":
            return verb, [(ci, "modify", None)]
        if verb in ("refresh", "import"):
            return verb, [(ci, "modify", False)]
        if verb == "destroy":
            return verb, [(ci, "delete", True)]
        return verb, [UNKNOWN]
    return None, [UNKNOWN]


def _process_effects(exe: str, family: str, args: List[Word], seg: Segment) -> Tuple[Optional[str], List[Effect]]:
    vals = [a.value for a in args]
    if family == "python" or exe in ("ruby", "perl", "node"):
        inline = {"-c"} if family == "python" else ({"-e"} if exe == "node" else {"-c", "-e"})
        if any(v in inline for v in vals) or "-" in vals or seg.heredoc:
            return None, [UNKNOWN]
        return None, [("process", "execute", None)]
    if exe == "java":
        return None, [("process", "execute", None)]
    if exe in RUNNERS:
        return None, [("process", "execute", None)]
    return None, [UNKNOWN]
