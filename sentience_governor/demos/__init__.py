"""Packaged, runnable demos.

These live inside the installed package (not examples/) so they import
and run from any install method — pipx, pip-in-venv, or source — without
the operator needing to know a Python path (F-V6). Surfaced via
`sentience demo <name>`.
"""

from sentience_governor.demos.undeclared_intent import (
    SESSION_ID,
    build_session_events,
)

__all__ = ["SESSION_ID", "build_session_events"]
