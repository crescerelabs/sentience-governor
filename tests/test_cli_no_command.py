"""F-V2: bare `sentience` prints a helpful guide, not an argparse error.

A first-time operator who types `sentience` to explore should get
useful guidance and a zero exit code. An *invalid* subcommand must
still error via argparse (exit 2).
"""

import pytest

from sentience_governor.cli import ux


@pytest.fixture(autouse=True)
def _suppress_first_run(monkeypatch):
    # Isolate the guide behavior from the first-run flow.
    monkeypatch.setenv("SENTIENCE_NO_FIRST_RUN_PROMPT", "1")


def test_bare_sentience_prints_guide_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["sentience"])
    rc = ux.main()
    assert rc == 0

    out = capsys.readouterr().out
    # Lists the real subcommands… (pulse included — F-026-1 regression guard:
    # the v0.2.6 headline command must appear in the new-operator guide).
    for cmd in ("status", "list", "open", "analyze", "pulse", "profile", "init"):
        assert f"sentience {cmd}" in out
    # …and names `status` as the suggested first command.
    assert "Start with" in out
    assert "sentience status" in out


def test_invalid_subcommand_still_errors(monkeypatch):
    monkeypatch.setattr("sys.argv", ["sentience", "definitely-not-a-command"])
    with pytest.raises(SystemExit) as exc:
        ux.main()
    # argparse uses exit code 2 for argument errors.
    assert exc.value.code == 2


def test_valid_subcommand_still_dispatches(monkeypatch, capsys):
    # `list` with no captured sessions should run (rc 0) and not hit
    # the guide path.
    monkeypatch.setattr("sys.argv", ["sentience", "list"])
    rc = ux.main()
    assert rc == 0
    out = capsys.readouterr().out
    # The guide banner must NOT appear for a real command.
    assert "Run any command with -h for details" not in out
