"""F-V6: `sentience demo <name>` runs packaged demos from any install.

The synthetic session builder moved into the package
(sentience_governor/demos/) so the demo imports and runs inside the
installed venv — no system-python ModuleNotFoundError, no Python-path
knowledge required.
"""

import argparse

from sentience_governor.cli.ux import run_demo
from sentience_governor.demos import build_session_events


def _ns(name):
    return argparse.Namespace(demo_name=name)


def test_demo_undeclared_intent_renders_drift(capsys):
    rc = run_demo(_ns("undeclared-intent"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "20.7%" in out
    assert "slack.write_message" in out


def test_demo_closed_loop_renders_clean(capsys):
    rc = run_demo(_ns("closed-loop"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "100.0%" in out
    assert "no_token_data" not in out


def test_packaged_builder_is_importable_and_stable():
    events = build_session_events()
    # 1 intent + 4 turns * (scope + snapshot) = 9 events.
    assert len(events) == 9
    assert events[0]["event_type"] == "INTENT_DECLARED"
    # The undeclared turn carries the POL-001 violation.
    pol = [
        e for e in events
        if "POL-001" in e.get("policy_violations", [])
    ]
    assert len(pol) == 1
    assert pol[0]["payload"]["tool_id"] == "slack.write_message"
