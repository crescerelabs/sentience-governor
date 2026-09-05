"""Sentience Governor for Pydantic AI — Agent Execution Evidence.

Governance-relevant evidence of what an agent actually dispatched at
runtime, against the objective and scope it declared.

``SentienceGovernor`` opens one Sentience Governor session per Pydantic AI
run and closes it when the run ends. It currently carries the session
lifecycle only: declaration reading, tool classification and token capture
arrive in later checkpoints.

The distribution depends on ``sentience-governor``; the dependency never
runs the other way, and core acquires no Pydantic AI dependency.
"""

from importlib.metadata import PackageNotFoundError, version as _dist_version

from pydantic_ai_governor.capability import SentienceGovernor

_DISTRIBUTION = "pydantic-ai-governor"

try:
    # DERIVED, never hand-maintained. The single version authority is the
    # `version` field in this package's pyproject.toml; reading it back from
    # installed metadata means a second source cannot drift from the first.
    __version__ = _dist_version(_DISTRIBUTION)
except PackageNotFoundError:  # pragma: no cover - source tree, not installed
    # Running from a source checkout without an install. Report the absence
    # honestly rather than inventing a number that could disagree with the
    # distribution.
    __version__ = "unknown"

__all__ = ["SentienceGovernor", "__version__"]
