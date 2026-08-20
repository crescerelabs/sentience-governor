#!/usr/bin/env python3
"""Sentience MCP wrapper demo — live Airtable CRM tool-use loop with governance.

Runs a real Claude API tool-use loop through ``wrap_mcp_client`` against a
real Airtable base ("Governor CRM Sandbox"). Writes a governance trace to
``/tmp/sentience-airtable-demo.jsonl``. Cross-check the trace against (a)
Airtable's activity log on the sandbox base and (b) the Anthropic console's
usage log — the ``MEMORY_WRITE_ATTEMPT`` event's timestamp should match the
``Created At`` timestamp of the newly-created ``Snapshots`` row within a
few seconds.

This script is NOT a test. It is intentionally non-deterministic and
depends on two live credentials.

Operator guardrail
------------------
This demo writes to the ``Snapshots`` table on every run. Use a sandbox
base only. Do not point this at a production Airtable base.

Quick start
-----------
    pip install -e ".[demo]"
    export ANTHROPIC_API_KEY=sk-ant-...
    export AIRTABLE_API_KEY=pat...
    export AIRTABLE_BASE_ID=app...your-sandbox-base-id
    python examples/claude_airtable_demo.py
    sentience-cli /tmp/sentience-airtable-demo.jsonl

Environment overrides
---------------------
    ANTHROPIC_API_KEY            (required)  Claude API credentials
    AIRTABLE_API_KEY             (required)  PAT scoped to the sandbox base
    AIRTABLE_BASE_ID             (required)  Sandbox base ID (appXXX...)
    SENTIENCE_DEMO_SINK_PATH     (default: /tmp/sentience-airtable-demo.jsonl)
    SENTIENCE_DEMO_MODEL         (default: claude-sonnet-4-5)

Exit codes
----------
    0    success
    1    missing required env var, or missing anthropic/pyairtable deps
    2    Anthropic API error
    3    Airtable API error (auth, rate-limit, or base/table not found)
    130  KeyboardInterrupt
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Sentience imports (always present — runtime is the main package)
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.schema.events import ClassificationSource
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import FileSink, SinkWriter
from sentience_governor.wrapper.mcp import (
    ClassificationHint,
    SentienceMCPAdapter,
    wrap_mcp_client,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SINK_PATH = "/tmp/sentience-airtable-demo.jsonl"
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_ITERATIONS = 10  # safety cap to prevent runaway tool-use loops

# Table names in the Governor CRM Sandbox base. These are stable
# constants, not env vars — the base is locked by AIRTABLE_BASE_ID, and
# the table names are documented fixture schema. If an operator renames
# a table in Airtable, the demo fails loud with a clear pyairtable error.
CUSTOMERS_TABLE = "Customers"
USAGE_TABLE = "Usage"
SNAPSHOTS_TABLE = "Snapshots"

TASK_PROMPT = (
    "I need you to: "
    "(1) look up the customer 'Acme Corp' (id: cust-001), "
    "(2) fetch their Q1 2026 usage (quarter: 2026Q1), and "
    "(3) write a snapshot to the database summarising what you found "
    "(brief prose summary, 1-2 sentences). "
    "Use the tools I've given you. "
    "Tell me when you're done."
)


# ---------------------------------------------------------------------------
# AirtableCRM — concrete tool client backed by the sandbox base
#
# Uses pyairtable for the actual Airtable REST API calls. Each tool maps
# to one pyairtable operation. The wrapper sees this through the
# SentienceMCPAdapter and is unaware that the backing store is Airtable.
# ---------------------------------------------------------------------------

class AirtableCRM:
    """CRM-like facade over an Airtable sandbox base.

    Three operations: get a customer by ID, fetch usage records for a
    customer+quarter, write a snapshot record. Timestamps and snapshot
    keys are generated server-side so the model cannot fabricate them.
    """

    def __init__(self, api_key: str, base_id: str) -> None:
        # Lazy import so a missing pyairtable install fails at demo run
        # time rather than module import time.
        from pyairtable import Api

        self._api = Api(api_key)
        self._base = self._api.base(base_id)
        self._customers = self._base.table(CUSTOMERS_TABLE)
        self._usage = self._base.table(USAGE_TABLE)
        self._snapshots = self._base.table(SNAPSHOTS_TABLE)

    def get_customer(self, id: str) -> dict:
        """Return the customer record for the given Customer ID, or an
        empty dict if not found."""
        records = self._customers.all(
            formula=f"{{Customer ID}} = '{id}'",
            max_records=1,
        )
        if not records:
            return {"found": False, "id": id}
        fields = records[0].get("fields", {})
        return {
            "found": True,
            "id": fields.get("Customer ID"),
            "name": fields.get("Name"),
            "tier": fields.get("Tier"),
            "region": fields.get("Region"),
        }

    def fetch_usage(self, customer_id: str, quarter: str) -> dict:
        """Return the usage record for a customer in a given quarter."""
        records = self._usage.all(
            formula=(
                f"AND("
                f"{{Customer ID}} = '{customer_id}', "
                f"{{Quarter}} = '{quarter}'"
                f")"
            ),
            max_records=1,
        )
        if not records:
            return {"found": False, "customer_id": customer_id, "quarter": quarter}
        fields = records[0].get("fields", {})
        return {
            "found": True,
            "customer_id": fields.get("Customer ID"),
            "quarter": fields.get("Quarter"),
            "api_calls": fields.get("API Calls"),
            "storage_gb": fields.get("Storage GB"),
        }

    def write_snapshot_to_database(self, customer_id: str, summary: str) -> dict:
        """Create a Snapshot record. Snapshot key and Created At are
        generated server-side; the model only supplies customer_id and
        summary."""
        now = datetime.now(timezone.utc)
        # ISO 8601 with milliseconds, UTC Z — matches the trace's
        # timestamp_utc format for easy cross-reference.
        created_at = (
            now.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{now.microsecond // 1000:03d}Z"
        )
        key = f"snap-{customer_id}-{int(now.timestamp())}"
        created = self._snapshots.create(
            {
                "Snapshot Key": key,
                "Customer ID": customer_id,
                "Summary": summary,
                "Created At": created_at,
            }
        )
        return {
            "ok": True,
            "snapshot_key": key,
            "created_at": created_at,
            "airtable_record_id": created.get("id"),
        }


# ---------------------------------------------------------------------------
# Tool dispatcher and adapter
# ---------------------------------------------------------------------------

class ToolDispatcher:
    """Maps wrapper-side tool names (dotted) to AirtableCRM methods."""

    def __init__(self, crm: AirtableCRM) -> None:
        self._crm = crm
        self._tools = {
            "crm.get_customer": lambda args: self._crm.get_customer(**args),
            "crm.fetch_usage": lambda args: self._crm.fetch_usage(**args),
            "crm.write_snapshot_to_database": lambda args: self._crm.write_snapshot_to_database(**args),
        }

    def call_tool(self, name: str, args: dict) -> dict:
        if name not in self._tools:
            raise ValueError(f"unknown tool: {name}")
        return self._tools[name](args)


# ---------------------------------------------------------------------------
# Classification hook
#
# These classifications are
# demo-supplied metadata associated with the sandbox customer. They are
# NOT inferred from the Airtable response — the Governor has no way to
# introspect a base and derive sensitivity tiers from the data. A
# production integrator would either read a metadata column on the
# Airtable record or consult a separate classification registry.
# ---------------------------------------------------------------------------

# v0.2.3 Track 2 — token-tracking context.
#
# We store the most recent Anthropic response and the current LLM-turn
# id in a module-level dict so the classification hook (called per
# tool call by the wrapper) can populate the new token fields on the
# emitted CONTEXT_SNAPSHOT events. The main loop assigns a fresh turn
# id on every Anthropic call (see while-loop below); every tool call
# that fires from that turn shares the same turn id, so downstream
# consumers can dedupe by ``(session_id, llm_turn_id)`` before summing
# token counts. See docs/guide/sentience_governor.md "Token tracking
# (optional)" for the full aggregation contract.
_TURN_CONTEXT: dict = {
    "response": None,  # the last anthropic.Message
    "turn_id": None,   # opaque per-turn identity
}


def extract_classification(
    tool_name: str, args: dict, result: Any
) -> Optional[ClassificationHint]:
    """Return a ClassificationHint with demo-supplied values.

    All three tools touch customer-identifiable data on the sandbox's
    single enterprise-tier customer, so every call carries the same
    classification. Only the write tool populates write-specific fields.

    v0.2.3 Track 2: also populates LLM-token fields from the most recent
    Anthropic response (tracked in ``_TURN_CONTEXT`` by the main loop).
    Every tool call fired from one model turn shares the same usage
    + ``llm_turn_id``; consumers must dedupe by turn id before summing
    (see userdoc).
    """
    from sentience_governor.wrapper.token_extraction import extract_anthropic_usage

    is_write = tool_name == "crm.write_snapshot_to_database"

    # Pull token usage from the latest Anthropic response, if available.
    response = _TURN_CONTEXT.get("response")
    usage_dict = (
        extract_anthropic_usage(getattr(response, "usage", None))
        if response is not None
        else {}
    )
    model_id = getattr(response, "model", None) if response is not None else None

    return ClassificationHint(
        data_classifications=["customer_confidential"],
        classification_source=ClassificationSource.vendor,
        provenance=["airtable.governor_crm_sandbox"],
        retention_flags=["customer_data_30d"],
        context_size_tokens=len(str(result).split()) * 2,
        write_classification="customer_confidential" if is_write else None,
        retention_requested="30d" if is_write else None,
        # v0.2.3 Track 2 — LLM token accounting.
        **usage_dict,
        model_identifier=model_id,
        provider="anthropic",
        llm_turn_id=_TURN_CONTEXT.get("turn_id"),
    )


# ---------------------------------------------------------------------------
# Claude tool schemas
#
# Anthropic's tool naming rejects dots, so schemas use underscored names.
# CLAUDE_TO_WRAPPER_NAME below translates Claude's chosen name back to
# the dotted form the wrapper sees. Explicit map rather than regex
# substitution because some tool names have multiple underscores and
# naive replacement would misplace the dot.
# ---------------------------------------------------------------------------

CLAUDE_TOOLS = [
    {
        "name": "crm_get_customer",
        "description": (
            "Look up a customer by their Customer ID in the CRM. "
            "Returns name, tier, and region if found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Customer ID, e.g. 'cust-001'",
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "crm_fetch_usage",
        "description": (
            "Fetch usage metrics (API calls, storage) for a customer in "
            "a specific quarter. Returns the usage record if found."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "Customer ID, e.g. 'cust-001'",
                },
                "quarter": {
                    "type": "string",
                    "description": "Quarter identifier, e.g. '2026Q1'",
                },
            },
            "required": ["customer_id", "quarter"],
        },
    },
    {
        "name": "crm_write_snapshot_to_database",
        "description": (
            "Write a customer snapshot record to the CRM database. "
            "The snapshot key and created-at timestamp are generated "
            "server-side; you only need to provide the customer ID and a "
            "brief prose summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "Customer ID the snapshot is about",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief prose summary (1-2 sentences)",
                },
            },
            "required": ["customer_id", "summary"],
        },
    },
]

# Explicit Claude-name → wrapper-name mapping. Three entries, three
# tools, no regex magic.
CLAUDE_TO_WRAPPER_NAME = {
    "crm_get_customer": "crm.get_customer",
    "crm_fetch_usage": "crm.fetch_usage",
    "crm_write_snapshot_to_database": "crm.write_snapshot_to_database",
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_demo(
    anthropic_key: str,
    airtable_key: str,
    airtable_base_id: str,
    sink_path: Path,
    model: str,
) -> int:
    """Drive the Claude tool-use loop through wrap_mcp_client.

    Returns
    -------
    0 on a successful session, non-zero on Anthropic / Airtable error.
    """
    # Lazy imports so missing extras fail at demo run time with a clear
    # install hint, not at module import time.
    try:
        import anthropic
    except ImportError:
        print(
            "error: anthropic SDK is not installed.\n"
            "Install demo extras with: pip install -e \".[demo]\"",
            file=sys.stderr,
        )
        return 1

    try:
        import pyairtable  # noqa: F401 — constructor uses it via AirtableCRM
    except ImportError:
        print(
            "error: pyairtable is not installed.\n"
            "Install demo extras with: pip install -e \".[demo]\"",
            file=sys.stderr,
        )
        return 1

    sink_path.parent.mkdir(parents=True, exist_ok=True)

    # Sentience collaborators
    session_manager = SessionManager()
    cache = InProcessCache()
    sink = SinkWriter(FileSink(str(sink_path)))

    # Real Airtable client wired through the dispatcher + MCP adapter
    try:
        crm = AirtableCRM(api_key=airtable_key, base_id=airtable_base_id)
    except Exception as exc:
        print(f"error: Airtable client construction failed: {exc}", file=sys.stderr)
        return 3

    dispatcher = ToolDispatcher(crm)
    adapted = SentienceMCPAdapter(
        delegate=dispatcher,
        call_fn=lambda d, name, args: d.call_tool(name, args),
    )

    wrapped = wrap_mcp_client(
        target=adapted,
        session_manager=session_manager,
        cache=cache,
        sink_writer=sink,
        agent_id="claude-airtable-demo-v1",
        agent_version="0.1.0",
        vendor_id="sentience-demo",
        declared_capabilities=["crm.read", "crm.write"],
        owner_claim=os.environ.get("USER", "demo-operator"),
        stated_objective="Look up Acme Corp usage and write a Q1 snapshot to the CRM database",
        classification_hook=extract_classification,
    )

    # Anthropic client
    client = anthropic.Anthropic(api_key=anthropic_key)

    messages: list[dict] = [
        {"role": "user", "content": TASK_PROMPT},
    ]

    iterations = 0
    final_stop_reason: Optional[str] = None

    import uuid

    async with wrapped:
        while iterations < MAX_ITERATIONS:
            iterations += 1
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=1024,
                    tools=CLAUDE_TOOLS,
                    messages=messages,
                )
            except anthropic.APIError as exc:
                print(
                    f"error: Claude API error on iteration {iterations}: {exc}",
                    file=sys.stderr,
                )
                return 2

            # v0.2.3 Track 2 — turn boundary. Every tool call that
            # fires from this iteration shares the same llm_turn_id,
            # so downstream consumers can dedupe by turn id before
            # summing token counts. See userdoc "Aggregation warning".
            _TURN_CONTEXT["response"] = response
            _TURN_CONTEXT["turn_id"] = uuid.uuid4().hex

            final_stop_reason = response.stop_reason

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                print(
                    f"warning: unexpected stop_reason on iteration {iterations}: "
                    f"{response.stop_reason}",
                    file=sys.stderr,
                )
                break

            # Append assistant response, then build tool_result blocks.
            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict] = []
            had_airtable_error = False
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue

                claude_name = block.name
                wrapper_name = CLAUDE_TO_WRAPPER_NAME.get(claude_name)
                if wrapper_name is None:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"error: unknown tool '{claude_name}'",
                            "is_error": True,
                        }
                    )
                    continue

                try:
                    result = wrapped.send_tool_call(wrapper_name, block.input)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        }
                    )
                except Exception as exc:
                    # Airtable-layer exceptions propagate through the
                    # wrapper (fail-open contract). Report to Claude as a
                    # tool error so it can try to recover, but track so
                    # we exit non-zero at the end.
                    had_airtable_error = True
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"tool error: {exc}",
                            "is_error": True,
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

            if had_airtable_error:
                print(
                    "warning: at least one Airtable tool call failed; "
                    "session will exit non-zero",
                    file=sys.stderr,
                )

    if iterations >= MAX_ITERATIONS and final_stop_reason != "end_turn":
        print(
            f"warning: hit max iteration cap ({MAX_ITERATIONS}); "
            "session closed early",
            file=sys.stderr,
        )

    print()
    print(f"Governance trace written to: {sink_path}")
    print(f"View with: sentience-cli {sink_path}")
    print()
    print(
        f"Cross-check receipts:\n"
        f"  (a) open the 'Snapshots' table in Airtable — a new row should "
        f"exist with Created At timestamp matching\n"
        f"      the trace's MEMORY_WRITE_ATTEMPT event within a few seconds.\n"
        f"  (b) https://console.anthropic.com/ — usage page should show "
        f"an API call for this run."
    )
    print()
    print(f"Session closed after {iterations} iteration(s).")
    return 0


def _missing_env_vars(required: list[str]) -> list[str]:
    return [name for name in required if not os.environ.get(name, "").strip()]


def main() -> int:
    required = ["ANTHROPIC_API_KEY", "AIRTABLE_API_KEY", "AIRTABLE_BASE_ID"]
    missing = _missing_env_vars(required)
    if missing:
        print(
            "error: required environment variables are not set:\n"
            + "\n".join(f"  - {name}" for name in missing)
            + "\n\nSee the module docstring for setup instructions.",
            file=sys.stderr,
        )
        return 1

    anthropic_key = os.environ["ANTHROPIC_API_KEY"].strip()
    airtable_key = os.environ["AIRTABLE_API_KEY"].strip()
    airtable_base_id = os.environ["AIRTABLE_BASE_ID"].strip()

    sink_path = Path(
        os.environ.get("SENTIENCE_DEMO_SINK_PATH", DEFAULT_SINK_PATH)
    ).expanduser()
    model = os.environ.get("SENTIENCE_DEMO_MODEL", DEFAULT_MODEL)

    try:
        return asyncio.run(
            run_demo(
                anthropic_key=anthropic_key,
                airtable_key=airtable_key,
                airtable_base_id=airtable_base_id,
                sink_path=sink_path,
                model=model,
            )
        )
    except KeyboardInterrupt:
        print("\ninterrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
