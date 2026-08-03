"""First-run capture flow for the ``sentience`` CLI (v0.2.3 Track 1).

Triggered the first time the user runs any meaningful ``sentience``
subcommand after install. Writes one-shot state to
``~/.sentience/first-run.json`` and never re-asks.

Two paths:

* **Interactive (TTY):** prints a short welcome, prompts for email
  and optional name, POSTs to the launch-list endpoint on submit,
  records the outcome.
* **Non-interactive (CI / piped / non-TTY):** prints a one-line
  banner to stderr pointing at the hosted form, records the skip.

Hard guarantees:

* Re-invocation never re-prompts (state file is the latch).
* Skip is permanent (empty input on email = skipped forever).
* Network failure is non-fatal — prints a friendly message, writes
  state, and falls through to the user's intended command.
* Failures inside this module never block the user's intended
  command (defensive ``try/except`` at the entry point).

Skipped automatically for:

* Already-completed first-run state (state file exists)
* Non-TTY contexts (CI, piped input/output)
* When ``SENTIENCE_NO_FIRST_RUN_PROMPT`` is set in the environment
  (escape hatch for power users / scripted workflows)

"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import error as urllib_error
from urllib import request as urllib_request

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STATE_DIR = Path.home() / ".sentience"
_STATE_FILE = _STATE_DIR / "first-run.json"
_STATE_SCHEMA_VERSION = 1

_DEFAULT_ENDPOINT = "https://sync.getsentience.ai/v1/launch-list/subscribe"
_LAUNCH_LIST_URL_ENV = "SENTIENCE_LAUNCH_LIST_URL"
_NO_PROMPT_ENV = "SENTIENCE_NO_FIRST_RUN_PROMPT"

_CONSENT_TEXT_VERSION = "launch-list-v1"
_INSTALL_SOURCE_TTY = "cli_first_run"
_INSTALL_SOURCE_NON_TTY = "post_install_banner"

_HOSTED_FORM_URL = "https://getsentience.ai/launch-list"

_REQUEST_TIMEOUT_SECONDS = 5.0

# Operator-facing copy. Uses "hosted console" per the vocabulary
# discipline (operator-facing copy says "console", never "control
# plane") — matches the README's "hear when the hosted console ships"
# phrasing. (Adapted from track-1-email-capture.md §1.2.)
_WELCOME_BLOCK = """\
Welcome to Sentience.

We're building a hosted console on top of this open-tier wrapper.
Drop an email if you'd like to hear when it ships. Hit Enter to skip.
"""

_BANNER_LINE = (
    "Welcome to Sentience. Join the launch list at "
    "https://getsentience.ai/launch-list to hear when the "
    "hosted console ships."
)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def maybe_run_first_run_flow(*, package_version: Optional[str] = None) -> None:
    """Entry point — called once per CLI invocation from ``ux.main``.

    No-ops when:
      - ``SENTIENCE_NO_FIRST_RUN_PROMPT`` is set
      - The state file already exists
      - Anything goes wrong (any exception is swallowed; see
        :func:`_safe_call_first_run`)

    The user's intended command MUST run regardless of what happens
    inside this function.
    """
    if os.environ.get(_NO_PROMPT_ENV):
        return
    if _STATE_FILE.exists():
        return
    _safe_call_first_run(package_version=package_version)


def _safe_call_first_run(*, package_version: Optional[str]) -> None:
    """Run the first-run flow under a broad safety net.

    Any unexpected exception is caught and silenced. KeyboardInterrupt
    is re-raised so users can ^C out of the prompt without a stuck
    state file (we leave the prompt unanswered; they'll see it again
    next invocation).
    """
    try:
        _run_first_run_flow(package_version=package_version)
    except KeyboardInterrupt:
        # User aborted the prompt. Don't write state — they'll get the
        # prompt again next run, which is the right behavior.
        raise
    except Exception:
        # Defensive: never let a first-run-flow bug block a real command.
        # We don't even log here because we don't want to spam stderr
        # for the user's normal flow.
        return


def _run_first_run_flow(*, package_version: Optional[str]) -> None:
    """Choose interactive vs banner path based on TTY status."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        _run_interactive(package_version=package_version)
    else:
        _run_banner_only()


# ---------------------------------------------------------------------------
# Interactive path
# ---------------------------------------------------------------------------


def _run_interactive(*, package_version: Optional[str]) -> None:
    """Print welcome + capture email/name + POST + record state."""
    # Welcome to stderr so it doesn't pollute any stdout the user
    # might be piping (even though we already TTY-checked stdout, the
    # convention is "messaging on stderr, data on stdout").
    print(_WELCOME_BLOCK, file=sys.stderr)

    try:
        email = input("Email (or Enter to skip): ").strip()
    except EOFError:
        # stdin closed unexpectedly — treat as skip.
        _write_state(subscribed=False, skip_reason="eof")
        return

    if not email:
        _write_state(subscribed=False, skip_reason="user_skipped")
        print("Skipped. Run `sentience` again later if you change your mind.\n",
              file=sys.stderr)
        return

    try:
        name = input("Name (optional): ").strip()
    except EOFError:
        name = ""

    # Submit.
    submitted_at, network_error = _post_subscribe(
        email=email,
        display_name=name or None,
        install_source=_INSTALL_SOURCE_TTY,
        package_version=package_version,
    )

    if network_error:
        _write_state(subscribed=False, skip_reason="network_failure")
        print(
            f"Couldn't reach the server right now — you can subscribe "
            f"later at {_HOSTED_FORM_URL}.\n",
            file=sys.stderr,
        )
        return

    _write_state(subscribed=True, subscribed_at=submitted_at)
    print(
        "Thanks. We'll be in touch when the hosted console ships.\n",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Non-TTY (banner) path
# ---------------------------------------------------------------------------


def _run_banner_only() -> None:
    """Print the banner once and record the skip.

    Emits to stderr so it doesn't pollute pipelines like
    ``sentience open --latest | jq``.
    """
    print(_BANNER_LINE, file=sys.stderr)
    _write_state(subscribed=False, skip_reason="non_tty")


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------


def _write_state(
    *,
    subscribed: bool,
    subscribed_at: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> None:
    """Atomically write the first-run state file.

    Atomicity: write to a same-directory temp file then ``os.replace``
    onto the final path. Safe under concurrent ``sentience``
    invocations because only the rename winner is observable.
    """
    state: Dict[str, Any] = {
        "schema_version": _STATE_SCHEMA_VERSION,
        "first_run_completed_at": _utcnow_iso(),
        "subscribed": subscribed,
        "subscribed_at": subscribed_at,
        "skip_reason": skip_reason,
    }
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix="first-run.", suffix=".tmp", dir=str(_STATE_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _STATE_FILE)
    except Exception:
        # Best-effort cleanup on any failure path.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _utcnow_iso() -> str:
    """Return current UTC time as ISO-8601 with seconds precision + Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# HTTP submit
# ---------------------------------------------------------------------------


def _post_subscribe(
    *,
    email: str,
    display_name: Optional[str],
    install_source: str,
    package_version: Optional[str],
) -> Tuple[Optional[str], bool]:
    """POST the subscription to the launch-list endpoint.

    Returns ``(submitted_at_iso, network_error_flag)``:

    * ``(iso_string, False)`` on a 2xx response
    * ``(None, True)`` on any failure — connection refused, timeout,
      DNS, 4xx, 5xx, malformed response. The caller treats this as
      a soft failure and records ``skip_reason="network_failure"``.

    The endpoint URL is overridable via ``SENTIENCE_LAUNCH_LIST_URL``
    so tests can point at a local fake server.
    """
    url = os.environ.get(_LAUNCH_LIST_URL_ENV) or _DEFAULT_ENDPOINT

    payload: Dict[str, Any] = {
        "email": email,
        "install_source": install_source,
        "consent_text_version": _CONSENT_TEXT_VERSION,
    }
    if display_name:
        payload["display_name"] = display_name
    if package_version:
        payload["package_version"] = package_version

    body = json.dumps(payload).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib_request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if 200 <= status < 300:
                return (_utcnow_iso(), False)
            return (None, True)
    except urllib_error.HTTPError:
        return (None, True)
    except urllib_error.URLError:
        return (None, True)
    except (TimeoutError, OSError):
        return (None, True)


# ---------------------------------------------------------------------------
# Internal: re-export for tests
# ---------------------------------------------------------------------------


__all__ = ["maybe_run_first_run_flow"]
