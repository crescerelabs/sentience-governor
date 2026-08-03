"""v0.2.3 token-attribution demo — multi-tool-call attribution + dedupe-safe summary.

Constructs a realistic agent session: one LLM turn produces three tool
calls. All three CONTEXT_SNAPSHOT events from that turn carry the same
``llm_turn_id`` and the same token spend, because the tokens were burned
ONCE on the model API call that produced the three tool_use blocks.

Then derives a summary from the trace that demonstrates the v0.2.3
aggregation contract:

  * Naive ``SUM(llm_prompt_tokens)`` overcounts by 3× because the same
    spend appears on three events.
  * Deduping by ``(session_id, llm_turn_id)`` first, then summing,
    gets the correct total.

This is the load-bearing v0.2.3 demonstration — without ``llm_turn_id``
as an explicit attribution identity, downstream cost analytics on
multi-tool-call sessions would be silently wrong.

Run:

    python examples/v023_token_attribution_demo.py

Output:

    /tmp/v023-token-attribution-trace.jsonl  (the trace)
    Stdout: trace events + summary

See ``userdocs/sentience_governor.md`` "Token tracking (optional)" →
"Aggregation warning" for the full rule, and
the token-attribution contract for the
attribution-identity design rationale.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import defaultdict
from pathlib import Path

from sentience_governor.cache.cache import InProcessCache
from sentience_governor.schema.events import ClassificationSource
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import FileSink, SinkWriter
from sentience_governor.wrapper.mcp import (
    ClassificationHint,
    SentienceMCPAdapter,
    wrap_mcp_client,
)


# ---------------------------------------------------------------------
# A small fake CRM that the agent calls three times in one turn.
# In real usage this would be a Slack / Airtable / Postgres MCP server.
# ---------------------------------------------------------------------


class FakeCRM:
    def call_tool(self, tool_name: str, args: dict):
        if tool_name == "crm.get_customer":
            return {"id": args["id"], "name": "Acme Corp", "tier": "enterprise"}
        if tool_name == "crm.list_invoices":
            return {"invoices": [{"id": "INV-001", "amount": 12000}]}
        if tool_name == "crm.write_snapshot":
            return {"snapshot_id": "S-2026-Q1", "stored": True}
        return {"unknown_tool": tool_name}


# ---------------------------------------------------------------------
# The classification hook populates Track 2 token fields. In real usage
# you'd call ``extract_anthropic_usage(response.usage)`` after the
# Anthropic API call; here we hand-construct the values so the demo is
# offline.
#
# Critical: every tool call in the same LLM turn carries the SAME
# usage and the SAME ``llm_turn_id``. The tokens were burned ONCE on
# the API call that produced all three tool_use blocks; we attach
# the same record to every emitted event so consumers know they
# came from one turn.
# ---------------------------------------------------------------------


SIMULATED_TURN_TOKENS = {
    "llm_prompt_tokens": 4812,
    "llm_completion_tokens": 932,
    "llm_cached_read_tokens": 1200,
    "llm_cached_write_tokens": 240,
    "model_identifier": "claude-sonnet-4-5",
    "provider": "anthropic",
}
SIMULATED_TURN_ID = uuid.uuid4().hex[:12]


def hint_factory(tool_name: str, args: dict, result):
    """Returns a ClassificationHint populated with v0.2.3 token fields."""
    is_write = tool_name == "crm.write_snapshot"
    return ClassificationHint(
        data_classifications=["customer_confidential"],
        classification_source=ClassificationSource.vendor,
        provenance=["fake_crm.demo"],
        retention_flags=["customer_data_30d"],
        write_classification="customer_confidential" if is_write else None,
        retention_requested="30d" if is_write else None,
        # v0.2.3 token attribution
        llm_turn_id=SIMULATED_TURN_ID,
        **SIMULATED_TURN_TOKENS,
    )


# ---------------------------------------------------------------------
# Run the session
# ---------------------------------------------------------------------


async def run_session(sink_path: Path) -> str:
    sm = SessionManager()
    cache = InProcessCache()
    sink = SinkWriter(FileSink(str(sink_path)))

    adapted = SentienceMCPAdapter(
        delegate=FakeCRM(),
        call_fn=lambda c, n, a: c.call_tool(n, a),
    )

    wrapped = wrap_mcp_client(
        target=adapted,
        session_manager=sm,
        cache=cache,
        sink_writer=sink,
        agent_id="acme-q1-reporter",
        agent_version="0.2.3",
        vendor_id="crescere-demo",
        declared_capabilities=["crm.read", "crm.write"],
        owner_claim="demo-operator",
        stated_objective="Generate Q1 customer report for Acme Corp",
        classification_hook=hint_factory,
    )

    async with wrapped:
        # Three tool calls, all from one LLM turn.
        wrapped.send_tool_call("crm.get_customer", {"id": "C-123"})
        wrapped.send_tool_call("crm.list_invoices", {"customer_id": "C-123"})
        wrapped.send_tool_call(
            "crm.write_snapshot", {"customer_id": "C-123", "quarter": "2026-Q1"}
        )

    return SIMULATED_TURN_ID


# ---------------------------------------------------------------------
# Summary derivation — demonstrates the aggregation contract
# ---------------------------------------------------------------------


def summarize(sink_path: Path) -> dict:
    """Build an operator-shaped summary from the JSONL trace.

    The load-bearing piece: when summing token spend across events,
    dedupe by ``(session_id, llm_turn_id)``. Sessions where one LLM
    turn produces multiple tool calls would otherwise have inflated
    token totals.
    """
    events = [
        json.loads(line)
        for line in sink_path.read_text().splitlines()
        if line.strip()
    ]
    if not events:
        return {}

    session_id = events[0].get("session_id", "<unknown>")

    # Event-type counts
    event_type_counts: dict = defaultdict(int)
    for e in events:
        event_type_counts[e["event_type"]] += 1

    # Tool calls = SCOPE_ASSERTED events
    tool_calls = [e for e in events if e["event_type"] == "SCOPE_ASSERTED"]

    # Aggregate violations + advisory flags
    all_violations: list = []
    all_flags: list = []
    for e in events:
        all_violations.extend(e.get("policy_violations", []))
        all_flags.extend(e.get("advisory_flags", []))

    # Token aggregation — TWO ways, to show the contract
    naive_total_prompt = 0
    naive_total_completion = 0
    seen_turn_ids: set = set()
    deduped_total_prompt = 0
    deduped_total_completion = 0
    deduped_total_cached_read = 0
    deduped_total_cached_write = 0

    for e in events:
        payload = e.get("payload", {})
        prompt = payload.get("llm_prompt_tokens")
        completion = payload.get("llm_completion_tokens")
        cached_read = payload.get("llm_cached_read_tokens")
        cached_write = payload.get("llm_cached_write_tokens")
        turn_id = payload.get("llm_turn_id")

        if prompt is not None:
            # NAIVE — sums every event's tokens (overcounts on multi-tool turns)
            naive_total_prompt += prompt
            naive_total_completion += completion or 0

            # CORRECT — dedupe by (session_id, llm_turn_id)
            if turn_id and turn_id not in seen_turn_ids:
                seen_turn_ids.add(turn_id)
                deduped_total_prompt += prompt
                deduped_total_completion += completion or 0
                deduped_total_cached_read += cached_read or 0
                deduped_total_cached_write += cached_write or 0

    return {
        "session_id": session_id,
        "events": {
            "total": len(events),
            "by_type": dict(event_type_counts),
        },
        "tool_calls": {
            "total": len(tool_calls),
            "tools_invoked": [tc["payload"]["tool_id"] for tc in tool_calls],
        },
        "llm_turns": {
            "total": len(seen_turn_ids),
            "turn_ids": sorted(seen_turn_ids),
        },
        "tokens_naive_sum_WRONG": {
            "prompt": naive_total_prompt,
            "completion": naive_total_completion,
            "comment": (
                "Sums every event's tokens. Inflates by N× when one LLM "
                "turn produces N tool calls."
            ),
        },
        "tokens_deduped_by_turn_CORRECT": {
            "prompt": deduped_total_prompt,
            "completion": deduped_total_completion,
            "cached_read": deduped_total_cached_read,
            "cached_write": deduped_total_cached_write,
            "comment": (
                "Dedupes by (session_id, llm_turn_id). This is the v0.2.3 "
                "aggregation contract."
            ),
        },
        "governance": {
            "policy_violations": all_violations,
            "advisory_flags": all_flags,
        },
    }


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


async def main() -> None:
    sink_path = Path("/tmp/v023-token-attribution-trace.jsonl")
    if sink_path.exists():
        sink_path.unlink()

    await run_session(sink_path)

    print("=" * 72)
    print(f"  TRACE — {sink_path}")
    print("=" * 72)
    for line in sink_path.read_text().splitlines():
        event = json.loads(line)
        print(f"\n  {event['event_type']} (seq {event['event_sequence_number']})")
        payload_str = json.dumps(event["payload"], indent=4)
        print(
            "  payload: "
            + (payload_str[:800] + "..." if len(payload_str) > 800 else payload_str)
        )
        if event.get("policy_violations"):
            print(f"  ⚠ violations: {event['policy_violations']}")
        if event.get("advisory_flags"):
            print(f"  ⚠ flags: {event['advisory_flags']}")

    print()
    print("=" * 72)
    print("  SUMMARY — derived from trace (demonstrates v0.2.3 aggregation contract)")
    print("=" * 72)
    summary = summarize(sink_path)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
