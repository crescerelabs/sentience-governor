"""F-V5 (cheap half): bundled showcase trace + improved no_token_data copy.

A fresh-install operator whose first analysis returns no_token_data
(the common Claude Code shape today) should be pointed at a concrete
next step — a populated example they can run immediately. We bundle the
closed-loop showcase trace inside the package and expose it via
`sentience analyze undeclared-intent --showcase`.
"""

import argparse

from sentience_governor.analyze.renderers import render_cli
from sentience_governor.cli import ux
from sentience_governor.cli.ux import (
    _bundled_showcase_path,
    run_analyze_undeclared_intent,
)


def _analyze_ns(**kw):
    base = dict(
        target=None, latest=False, showcase=False,
        json=False, save=False, no_prompt=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_bundled_showcase_path_exists():
    p = _bundled_showcase_path()
    assert p is not None
    assert p.is_file()
    assert p.name.endswith(".jsonl")


def test_showcase_flag_renders_populated_analysis(capsys):
    rc = run_analyze_undeclared_intent(_analyze_ns(showcase=True))
    out = capsys.readouterr().out
    assert rc == 0
    # The bundled trace is a clean closed-loop session: 100% declared.
    assert "Total compute" in out
    assert "100.0%" in out
    # It must NOT hit the no_token_data path.
    assert "no_token_data" not in out


def test_showcase_flag_json_mode(capsys):
    rc = run_analyze_undeclared_intent(_analyze_ns(showcase=True, json=True))
    out = capsys.readouterr().out
    assert rc == 0
    import json as _json
    data = _json.loads(out)
    assert data["status"] == "ok"
    assert data["total_tokens"] > 0


def test_no_token_data_message_leads_with_live_session_cause():
    # FIX-1 (v0.2.8): the empty state names the dominant real cause
    # (session may still be running; data lands at SessionEnd) instead
    # of pointing at --showcase / unwired hooks.
    result = {
        "session_id": "abc12345-xxx",
        "status": "no_token_data",
        "session_has_declared_intent": False,
        "total_tokens": 0,
        "undeclared_tokens": 0,
        "declared_tokens": 0,
        "undeclared_ratio": 0.0,
        "undeclared_percent": 0.0,
        "undeclared_turn_count": 0,
        "total_turn_count": 0,
        "undeclared_turns": [],
        "warnings": [],
        "unpaired_event_count": 0,
        "untokened_pair_count": 0,
        "dedupe_conflict_count": 0,
        "malformed_event_count": 0,
    }
    out = render_cli(result)
    assert "may still be running" in out
    assert "sentience init claude-code" in out
