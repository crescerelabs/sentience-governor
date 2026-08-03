"""Step 7 acceptance: CLI Trace Viewer snapshot test for Flow B."""

import io
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SNAPSHOTS = Path(__file__).parent / "snapshots"


class TestCLIFlowBSnapshot:
    """CLI output for Flow B MUST exactly match the snapshot fixture."""

    def test_flow_b_snapshot(self):
        snapshot_path = SNAPSHOTS / "cli_flow_b_output.txt"
        assert snapshot_path.exists(), "Snapshot fixture not found"
        expected = snapshot_path.read_text()

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sentience_governor.cli.viewer",
                str(FIXTURES / "golden_trace_flow_b.json"),
                "--no-colour",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert result.stdout == expected

    def test_flow_b_deterministic(self):
        """Running the CLI twice produces identical output."""
        result1 = subprocess.run(
            [
                sys.executable, "-m", "sentience_governor.cli.viewer",
                str(FIXTURES / "golden_trace_flow_b.json"), "--no-colour",
            ],
            capture_output=True, text=True,
        )
        result2 = subprocess.run(
            [
                sys.executable, "-m", "sentience_governor.cli.viewer",
                str(FIXTURES / "golden_trace_flow_b.json"), "--no-colour",
            ],
            capture_output=True, text=True,
        )
        assert result1.stdout == result2.stdout


class TestCLIFlowAClean:
    """Flow A should show all-clean output."""

    def test_flow_a_no_violations(self):
        result = subprocess.run(
            [
                sys.executable, "-m", "sentience_governor.cli.viewer",
                str(FIXTURES / "golden_trace_flow_a.json"), "--no-colour",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "Policy violations:     0" in result.stdout
        assert "All events clean" in result.stdout

    def test_flow_a_all_checkmarks(self):
        result = subprocess.run(
            [
                sys.executable, "-m", "sentience_governor.cli.viewer",
                str(FIXTURES / "golden_trace_flow_a.json"), "--no-colour",
            ],
            capture_output=True, text=True,
        )
        assert "⚠" not in result.stdout
        assert result.stdout.count("✓") == 5  # 5 events, all clean


class TestCLIRobustness:
    """Malformed JSON lines are skipped; interleaved sessions grouped correctly."""

    def test_skip_malformed_lines(self, tmp_path):
        import json
        ndjson = tmp_path / "mixed.ndjson"
        events = [
            {
                "event_id": "e1", "event_type": "AGENT_REGISTERED",
                "session_id": "s1", "event_sequence_number": 1,
                "agent_id": "test", "deployment_mode": "vendor_managed",
                "timestamp_utc": "2026-04-14T00:00:00.000Z",
                "primitive": "REGISTRATION",
                "payload": {
                    "agent_id": "test", "agent_version": "1.0",
                    "vendor_id": "v", "deployment_mode": "vendor_managed",
                    "declared_capabilities": [], "owner_claim": None,
                },
                "advisory_flags": [], "policy_violations": [],
                "simulated_consequence": None, "pass_through": True,
            }
        ]
        with open(ndjson, "w") as f:
            f.write("NOT JSON\n")
            f.write(json.dumps(events[0]) + "\n")
            f.write("ALSO NOT JSON\n")

        result = subprocess.run(
            [sys.executable, "-m", "sentience_governor.cli.viewer", str(ndjson), "--no-colour"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "SESSION:" in result.stdout
        assert "WARNING: skipping malformed JSON" in result.stderr
