"""v0.2.8.3 — Sync cloud telemetry sunset guards.

Locks the local-first cleanup so it cannot silently regress:
  * the ``sentience-sync`` command is a sunset stub (no telemetry);
  * the telemetry implementation modules are gone;
  * the Governor runtime does not import the sync package;
  * the kept pulse footer is the EMAIL-LIST CTA only — no telemetry.

The "Sentience Sync" email list (getsentience.ai/sentience-sync) is
deliberately preserved; these guards must NOT remove it.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest

from sentience_sync.cli import main as sync_main

_FORMER_SUBCOMMANDS = [
    [],
    ["init"],
    ["register"],
    ["run"],
    ["status"],
    ["aggregate"],
    ["update-check"],
]


class TestSentienceSyncStub:
    def test_prints_sunset_and_exits_zero(self, capsys):
        rc = sync_main([])
        out = capsys.readouterr().out
        assert rc == 0
        assert "sunset" in out.lower()
        assert "local-first" in out.lower()

    def test_all_former_subcommands_share_one_sunset_message(self, capsys):
        seen = set()
        for argv in _FORMER_SUBCOMMANDS:
            assert sync_main(argv) == 0
            seen.add(capsys.readouterr().out)
        assert len(seen) == 1, (
            "every former subcommand shape must print the same sunset notice"
        )


class TestTelemetryRemoved:
    @pytest.mark.parametrize(
        "module",
        ["aggregator", "config", "identity", "orchestration", "state", "transport"],
    )
    def test_telemetry_module_is_gone(self, module):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"sentience_sync.{module}")

    def test_governor_runtime_does_not_import_sync(self):
        # Fresh interpreter: importing the Governor must not pull in the
        # sync package as a side effect (the one-way import boundary).
        code = (
            "import sentience_governor.cli.ux, "
            "sentience_governor.analyze.pulse, "
            "sentience_governor.event_builder.builder; "
            "import sys; "
            "assert 'sentience_sync' not in sys.modules, "
            "'governor must not import sentience_sync'; "
            "print('OK')"
        )
        r = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr


class TestEmailFooterPreserved:
    def test_footer_is_email_list_only_no_telemetry(self):
        from sentience_governor.analyze import renderers

        footer = (
            renderers._PULSE_SYNC_FOOTER_HEADING
            + " "
            + renderers._PULSE_SYNC_FOOTER_LINK
        ).lower()
        # The email-list CTA is preserved …
        assert "getsentience.ai/sentience-sync" in footer
        assert "weekly via email" in footer
        # … and carries no cloud-telemetry verbs.
        for verb in (
            "register",
            "upload",
            "aggregated",
            "sentience cloud",
            "installation_id",
        ):
            assert verb not in footer
