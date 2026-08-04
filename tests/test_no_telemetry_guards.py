"""No-telemetry guards. Re-homed from ``test_sync_sunset.py`` at v0.3.0.1 CP1.

These three properties are what the public README promises: no telemetry, no
usage beacon, nothing leaving the machine on the governance path. They were
originally asserted alongside tests for the ``sentience-sync`` sunset stub, and
that stub is removed at CP2.

**They are re-homed here, and proven green, BEFORE anything is deleted.**
Deleting the old file wholesale would have silently dropped the guarantee: the
stub's own tests are disposable, these are not.

Deliberately imports **nothing** from ``sentience_sync``. The old module did
(``from sentience_sync.cli import main``), which is exactly why these
assertions could not have survived the package's removal in place.

The "Sentience Sync" **email list** (``getsentience.ai/sentience-sync``) is a
separate, live product surface and is deliberately preserved. These guards must
never be read as licence to remove it.
"""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


class TestTelemetryRemoved:
    @pytest.mark.parametrize(
        "module",
        ["aggregator", "config", "identity", "orchestration", "state", "transport"],
    )
    def test_telemetry_module_is_gone(self, module):
        """The cloud-telemetry implementation modules are not importable.

        Passes both before and after CP2. Before, because the modules were
        deleted at v0.2.8.3 while the package remained; after, because the
        package itself is gone. The assertion is the same either way: this
        code must not come back.
        """
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"sentience_sync.{module}")

    def test_governor_runtime_does_not_import_sync(self):
        # Fresh interpreter: importing the Governor must not pull in the sync
        # package as a side effect (the one-way import boundary).
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
