"""Sentience Sync — SUNSET (v0.2.8.3).

The experimental Sync cloud-telemetry surface (register / run / upload /
update-check) was removed from the supported Governor product in v0.2.8.3.
Sentience Governor is local-first: governance runs entirely on the operator's
machine. This package remains only as a thin sunset stub (see ``cli.py``) so
the ``sentience-sync`` entry point prints a clear notice instead of failing.

Optional cloud / control-plane capabilities may return later as part of the MCP
roadmap. The "Sentience Sync" email list (getsentience.ai/sentience-sync) is a
separate, still-active product-updates list and is unaffected.
"""

__version__ = "0.1.0"
