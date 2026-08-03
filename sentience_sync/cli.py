"""sentience-sync — SUNSET (v0.2.8.3).

The experimental Sync cloud-telemetry CLI (the former ``init`` / ``register`` /
``run`` / ``status`` / ``aggregate`` / ``update-check`` subcommands) was removed
from the supported Governor product in v0.2.8.3. Sentience Governor is
local-first: all governance runs on the operator's machine.

This module is a thin stub. The ``sentience-sync`` entry point now prints a
clear sunset notice for ANY invocation (including every former subcommand) and
exits 0, so existing muscle memory gets a helpful message instead of a broken
command. The telemetry implementation (orchestration, transport, identity,
aggregator, config, state) was deleted in v0.2.8.3.

Note: the "Sentience Sync" email list (getsentience.ai/sentience-sync) is a
SEPARATE, still-active product-updates list and is unaffected by this sunset.
"""

from __future__ import annotations

from typing import Optional, Sequence

_SUNSET_MESSAGE = (
    "sentience-sync has been sunset in v0.2.8.3.\n"
    "Sentience Governor is local-first — everything runs on your machine.\n"
    "Optional cloud / control-plane capabilities may return later as part of\n"
    "the MCP roadmap. Your local governance (pulse, status, profile,\n"
    "violations, intent, Claude Code capture) is unaffected."
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the sunset notice and exit 0, regardless of arguments.

    ``argv`` is accepted and ignored so that any former invocation shape
    (bare ``sentience-sync`` or ``sentience-sync <subcommand> ...``) lands on
    the same notice. The console-script wrapper calls this with no arguments
    and uses the return value as the process exit code.
    """
    print(_SUNSET_MESSAGE)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
