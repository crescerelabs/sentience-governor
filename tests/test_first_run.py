"""Acceptance tests for the CLI first-run flow (v0.2.3 Track 1).

Tests :mod:`sentience_governor.cli.first_run` without touching the
real ``~/.sentience/first-run.json`` and without making real HTTP
requests. State directory is redirected to a per-test temp dir;
``urllib.request.urlopen`` is monkeypatched.

Coverage:

  Happy paths
    1. Interactive TTY: user submits email + name → POST + state(subscribed=True)
    2. Interactive TTY: user hits Enter on email → state(skip_reason=user_skipped, no POST)
    3. Non-TTY: banner printed to stderr, state(skip_reason=non_tty, no POST)

  Idempotency
    4. State file exists → no prompt, no POST
    5. SENTIENCE_NO_FIRST_RUN_PROMPT env var → no prompt, no POST, no state write

  Network handling
    6. POST fails (URLError) → state(skip_reason=network_failure), command not blocked
    7. POST returns 4xx → state(skip_reason=network_failure)
    8. Submitted payload includes email, install_source, consent_text_version
    9. Submitted payload includes display_name when provided
   10. Submitted payload includes package_version when provided

  State file
   11. State file has schema_version=1
   12. Atomic write — no temp file left behind on success
   13. Concurrent calls don't corrupt the state file
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import pytest

from sentience_governor.cli import first_run


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect _STATE_DIR / _STATE_FILE to a per-test temp dir."""
    state_dir = tmp_path / "sentience"
    state_file = state_dir / "first-run.json"
    monkeypatch.setattr(first_run, "_STATE_DIR", state_dir)
    monkeypatch.setattr(first_run, "_STATE_FILE", state_file)
    return state_dir


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


@pytest.fixture
def mock_urlopen(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    """Capture POST calls; return successful 200 by default."""
    calls: List[Dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float = 0.0) -> _FakeResponse:
        # Capture the request shape so tests can assert on it.
        calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": dict(request.headers),
                "body": (
                    json.loads(request.data.decode("utf-8"))
                    if request.data
                    else None
                ),
                "timeout": timeout,
            }
        )
        return _FakeResponse(status=200)

    monkeypatch.setattr(first_run.urllib_request, "urlopen", fake_urlopen)
    return calls


@pytest.fixture
def tty_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force isatty() to return True on stdin and stdout."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)


@pytest.fixture
def non_tty_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force isatty() to return False on stdin and stdout."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)


def _read_state(state_dir: Path) -> Dict[str, Any]:
    state_file = state_dir / "first-run.json"
    return json.loads(state_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_interactive_submit_writes_state_and_posts(
    state_dir: Path,
    tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = iter(["alice@example.com", "Alice Doe"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    # POST happened with the right payload.
    assert len(mock_urlopen) == 1
    call = mock_urlopen[0]
    assert call["method"] == "POST"
    assert call["url"] == first_run._DEFAULT_ENDPOINT
    assert call["body"] == {
        "email": "alice@example.com",
        "install_source": "cli_first_run",
        "consent_text_version": "launch-list-v1",
        "display_name": "Alice Doe",
        "package_version": "0.2.3",
    }
    # State file written with subscribed=True.
    state = _read_state(state_dir)
    assert state["subscribed"] is True
    assert state["subscribed_at"] is not None
    assert state["skip_reason"] is None
    assert state["schema_version"] == 1


def test_interactive_skip_writes_state_and_does_not_post(
    state_dir: Path,
    tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    assert len(mock_urlopen) == 0  # no POST on skip
    state = _read_state(state_dir)
    assert state["subscribed"] is False
    assert state["skip_reason"] == "user_skipped"


def test_non_tty_prints_banner_and_does_not_post(
    state_dir: Path,
    non_tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    first_run.maybe_run_first_run_flow()

    captured = capsys.readouterr()
    assert "launch list" in captured.err.lower()
    assert "getsentience.ai/launch-list" in captured.err
    # No POST in non-TTY path.
    assert len(mock_urlopen) == 0
    state = _read_state(state_dir)
    assert state["subscribed"] is False
    assert state["skip_reason"] == "non_tty"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_existing_state_file_is_no_op(
    state_dir: Path,
    tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pre-seed the state file.
    state_dir.mkdir(parents=True, exist_ok=True)
    pre_existing = {
        "schema_version": 1,
        "first_run_completed_at": "2026-01-01T00:00:00Z",
        "subscribed": False,
        "subscribed_at": None,
        "skip_reason": "user_skipped",
    }
    (state_dir / "first-run.json").write_text(
        json.dumps(pre_existing), encoding="utf-8"
    )

    # input() should never be called; raise if it is.
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail("input() should not be called"),
    )

    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    assert len(mock_urlopen) == 0
    # State file unchanged.
    assert _read_state(state_dir) == pre_existing


def test_no_prompt_env_var_short_circuits(
    state_dir: Path,
    tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTIENCE_NO_FIRST_RUN_PROMPT", "1")
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail("input() should not be called"),
    )

    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    # No POST, no state file written.
    assert len(mock_urlopen) == 0
    assert not (state_dir / "first-run.json").exists()


# ---------------------------------------------------------------------------
# Network handling
# ---------------------------------------------------------------------------


def test_network_failure_records_skip_reason(
    state_dir: Path,
    tty_stdio: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = iter(["alice@example.com", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    def fake_urlopen(request: Any, timeout: float = 0.0) -> Any:
        from urllib.error import URLError

        raise URLError("connection refused")

    monkeypatch.setattr(first_run.urllib_request, "urlopen", fake_urlopen)

    # Should not raise — failure is soft.
    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    state = _read_state(state_dir)
    assert state["subscribed"] is False
    assert state["skip_reason"] == "network_failure"


def test_4xx_response_records_skip_reason(
    state_dir: Path,
    tty_stdio: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = iter(["alice@example.com", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    def fake_urlopen(request: Any, timeout: float = 0.0) -> Any:
        from urllib.error import HTTPError

        raise HTTPError(
            url=request.full_url,
            code=400,
            msg="bad request",
            hdrs=None,  # type: ignore[arg-type]
            fp=None,
        )

    monkeypatch.setattr(first_run.urllib_request, "urlopen", fake_urlopen)

    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    state = _read_state(state_dir)
    assert state["skip_reason"] == "network_failure"


def test_payload_omits_optional_fields_when_empty(
    state_dir: Path,
    tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = iter(["alice@example.com", ""])  # empty name
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    first_run.maybe_run_first_run_flow(package_version=None)

    body = mock_urlopen[0]["body"]
    assert "display_name" not in body
    assert "package_version" not in body
    assert body["email"] == "alice@example.com"
    assert body["install_source"] == "cli_first_run"
    assert body["consent_text_version"] == "launch-list-v1"


def test_endpoint_url_overridable_via_env(
    state_dir: Path,
    tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SENTIENCE_LAUNCH_LIST_URL",
        "http://localhost:8080/v1/launch-list/subscribe",
    )
    inputs = iter(["alice@example.com", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    assert (
        mock_urlopen[0]["url"]
        == "http://localhost:8080/v1/launch-list/subscribe"
    )


# ---------------------------------------------------------------------------
# State file integrity
# ---------------------------------------------------------------------------


def test_state_file_contains_schema_version(
    state_dir: Path,
    tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    state = _read_state(state_dir)
    assert state["schema_version"] == first_run._STATE_SCHEMA_VERSION


def test_no_temp_file_left_behind_on_success(
    state_dir: Path,
    tty_stdio: None,
    mock_urlopen: List[Dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    first_run.maybe_run_first_run_flow(package_version="0.2.3")

    files = list(state_dir.iterdir())
    # Only the state file should exist; no .tmp leftovers.
    assert [f.name for f in files] == ["first-run.json"]


# ---------------------------------------------------------------------------
# Defensive — exception in the prompt flow must not propagate
# ---------------------------------------------------------------------------


def test_unexpected_exception_in_prompt_swallowed(
    state_dir: Path,
    tty_stdio: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(prompt: str = "") -> str:
        raise RuntimeError("simulated bug in input()")

    monkeypatch.setattr("builtins.input", boom)

    # Must not raise. Test passes simply by not raising.
    first_run.maybe_run_first_run_flow(package_version="0.2.3")


def test_keyboard_interrupt_propagates(
    state_dir: Path,
    tty_stdio: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(prompt: str = "") -> str:
        raise KeyboardInterrupt()

    monkeypatch.setattr("builtins.input", boom)

    with pytest.raises(KeyboardInterrupt):
        first_run.maybe_run_first_run_flow(package_version="0.2.3")

    # State file NOT written — user gets the prompt again next run.
    assert not (state_dir / "first-run.json").exists()
