"""Analyzer modules — derived metrics computed from v0.2.3+ trace data.

Each analyzer is a pure-function module that reads governance event
lists and returns structured results. Analyzers do not perform I/O,
do not read environment state, do not mutate inputs, and produce
byte-stable output for identical inputs. See per-module docstrings
for the specific guarantees.

Two analyzers ship today:

* :mod:`undeclared_intent` (v0.2.4) — how much of a session's compute
  attributed to turns that touched execution outside the session's
  declared operational intent.

* :mod:`policy_violation_burn_rate` (v0.2.6 CP1) — per-rule
  attribution of compute associated with turns where one or more
  policy rules (POL-001 through POL-005) fired.

Renderers live in :mod:`renderers`. CLI handlers (which write reports
to disk and prompt-fire) live in :mod:`sentience_governor.cli.ux`.

Future analyzers (v0.2.7+ candidates) include the retry-tax analyzer
(TOOL_CALL_FAILED-driven) and cross-session pattern detection.
"""

from sentience_governor.analyze.policy_violation_burn_rate import (
    compute_policy_violation_burn_rate,
)
from sentience_governor.analyze.pulse import compute_pulse
from sentience_governor.analyze.renderers import (
    render_burn_rate_cli,
    render_burn_rate_markdown,
    render_cli,
    render_markdown_report,
    render_pulse_cli,
    render_pulse_markdown,
)
from sentience_governor.analyze.undeclared_intent import (
    compute_undeclared_intent_spend,
)

__all__ = [
    "compute_undeclared_intent_spend",
    "compute_policy_violation_burn_rate",
    "compute_pulse",
    "render_cli",
    "render_markdown_report",
    "render_burn_rate_cli",
    "render_burn_rate_markdown",
    "render_pulse_cli",
    "render_pulse_markdown",
]
