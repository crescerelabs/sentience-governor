#!/usr/bin/env python3
"""Sentience MCP wrapper demo — live Claude tool-use loop with governance.

Runs a real Claude API tool-use loop through ``wrap_mcp_client`` and
writes a governance trace to ``/tmp/sentience-demo.jsonl``. Pipe the
sink file into ``sentience-cli`` to visualise what Claude actually did.

This script is NOT a test. It is intentionally non-deterministic and
exists to demonstrate the wrapper's public API against a real LLM.


Quick start
-----------
    pip install -e ".[demo]"
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/claude_demo.py
    sentience-cli /tmp/sentience-demo.jsonl

Environment overrides
---------------------
    ANTHROPIC_API_KEY            (required)
    SENTIENCE_DEMO_SINK_PATH     (default: /tmp/sentience-demo.jsonl)
    SENTIENCE_DEMO_MODEL         (default: claude-sonnet-4-5)

Exit codes
----------
    0    success
    1    missing ANTHROPIC_API_KEY
    2    Anthropic API error
    130  KeyboardInterrupt
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

# Sentience imports (always present — runtime is the main package)
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.schema.events import ClassificationSource
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import FileSink, SinkWriter
from sentience_governor.wrapper.mcp import (
    ClassificationHint,
    MCPClientLike,
    SentienceMCPAdapter,
    wrap_mcp_client,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SINK_PATH = "/tmp/sentience-demo.jsonl"
DEFAULT_MODEL = "claude-sonnet-4-5"
MAX_ITERATIONS = 10  # safety cap to prevent runaway tool-use loops

TASK_PROMPT = (
    "I need you to: "
    "(1) look up the customer 'Acme Corp' (id: cust-001), "
    "(2) fetch their Q1 2026 usage, and "
    "(3) upsert a snapshot row into the vector store with the results. "
    "Use the tools I've given you. "
    "Tell me when you're done."
)


# ---------------------------------------------------------------------------
# Fake tool implementations
#
# These return canned responses with classification metadata in a
# conventional ``_metadata`` key. The classification_hook reads this key
# and turns it into a ClassificationHint that flows into the wrapper's
# emitted CONTEXT_SNAPSHOT and MEMORY_WRITE_ATTEMPT events.
#
# The _metadata convention is specific to this demo. A real integration
# would define its own convention — the hook is how the caller bridges
# whatever metadata format their tools use to the ClassificationHint
# dataclass the wrapper expects.
# ---------------------------------------------------------------------------

def fake_crm_get_customer(id: str) -> dict:
    return {
        "data": {
            "id": id,
            "name": "Acme Corp",
            "tier": "enterprise",
            "primary_contact": "alex@acme.example",
        },
        "_metadata": {
            "classifications": ["internal"],
            "source": "vendor",
            "retention": ["may-persist"],
            "size_tokens": 850,
        },
    }


def fake_crm_fetch_usage(customer_id: str, window: str) -> dict:
    return {
        "data": {
            "customer_id": customer_id,
            "window": window,
            "total_calls": 12_450,
            "active_users": 87,
            "peak_day": "2026-03-14",
        },
        "_metadata": {
            "classifications": ["internal"],
            "source": "vendor",
            "retention": ["may-persist"],
            "size_tokens": 1200,
        },
    }


def fake_vector_store_upsert(row: dict) -> dict:
    return {
        "data": {
            "ok": True,
            "id": row.get("id", "unknown"),
            "indexed_at": "2026-04-15T12:34:56Z",
        },
        "_metadata": {
            "classifications": ["internal"],
            "source": "vendor",
            "retention": ["may-persist"],
            "size_tokens": 640,
            # Memory-write fields (consumed by the hook for MEMORY_WRITE_ATTEMPT)
            "write_classification": "internal",
            "retention_requested": "30_days",
        },
    }


# ---------------------------------------------------------------------------
# Tool dispatcher and adapter
# ---------------------------------------------------------------------------

class ToolDispatcher:
    """Maps wrapper-side tool names (dotted) to the fake tool functions."""

    def __init__(self) -> None:
        self._tools = {
            "crm.get_customer": lambda args: fake_crm_get_customer(**args),
            "crm.fetch_usage": lambda args: fake_crm_fetch_usage(**args),
            "vector_store.upsert": lambda args: fake_vector_store_upsert(**args),
        }

    def call_tool(self, name: str, args: dict) -> dict:
        if name not in self._tools:
            raise ValueError(f"unknown tool: {name}")
        return self._tools[name](args)


# ---------------------------------------------------------------------------
# Classification hook
#
# Reads the conventional _metadata key from fake tool responses and
# maps it onto a ClassificationHint. Fields the metadata didn't supply
# fall through to the wrapper's defaults via the ClassificationHint's
# Optional[T] = None semantics.
# ---------------------------------------------------------------------------

def extract_classification(
    tool_name: str, args: dict, result: Any
) -> Optional[ClassificationHint]:
    if not isinstance(result, dict):
        return None
    metadata = result.get("_metadata")
    if not metadata:
        return None
    return ClassificationHint(
        data_classifications=metadata.get("classifications"),
        classification_source=ClassificationSource.vendor,
        provenance=[tool_name.split(".")[0]],
        retention_flags=metadata.get("retention"),
        context_size_tokens=metadata.get("size_tokens"),
        write_classification=metadata.get("write_classification"),
        retention_requested=metadata.get("retention_requested"),
    )


# ---------------------------------------------------------------------------
# Claude tool schemas
#
# Anthropic's tool naming rejects dots, so the schema uses underscored
# names. The CLAUDE_TO_WRAPPER_NAME map below normalises Claude's chosen
# tool name back to the dotted form the wrapper sees. An explicit mapping
# is used rather than a regex (e.g. replace first underscore with dot)
# because vector_store_upsert has two underscores and naive replacement
# would produce vector.store_upsert instead of vector_store.upsert.
# ---------------------------------------------------------------------------

CLAUDE_TOOLS = [
    {
        "name": "crm_get_customer",
        "description": "Look up a customer by ID. Returns name, tier, contact info.",
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
        "description": "Fetch usage metrics for a customer over a time window.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string"},
                "window": {
                    "type": "string",
                    "description": "Time window, e.g. 'Q1-2026'",
                },
            },
            "required": ["customer_id", "window"],
        },
    },
    {
        "name": "vector_store_upsert",
        "description": "Upsert a row into the vector store. Returns the indexed id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "row": {
                    "type": "object",
                    "description": "The row to upsert. Must include an 'id' field.",
                },
            },
            "required": ["row"],
        },
    },
]

# Explicit mapping from Claude's tool naming (underscores) to the wrapper's
# dotted tool naming. Three tools, three entries — explicit beats clever.
CLAUDE_TO_WRAPPER_NAME = {
    "crm_get_customer": "crm.get_customer",
    "crm_fetch_usage": "crm.fetch_usage",
    "vector_store_upsert": "vector_store.upsert",
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run_demo(api_key: str, sink_path: Path, model: str) -> int:
    """Drive the Claude tool-use loop through wrap_mcp_client.

    Returns
    -------
    0 on a successful session, non-zero on Anthropic API error.
    """
    # Lazy import so a missing anthropic install only fails when the demo
    # actually tries to run, not at module import time.
    try:
        import anthropic
    except ImportError:
        print(
            "error: anthropic SDK is not installed.\n"
            "Install demo extras with: pip install -e \".[demo]\"",
            file=sys.stderr,
        )
        return 1

    sink_path.parent.mkdir(parents=True, exist_ok=True)

    # Sentience collaborators
    session_manager = SessionManager()
    cache = InProcessCache()
    sink = SinkWriter(FileSink(str(sink_path)))

    # Wire the fake tool dispatcher through SentienceMCPAdapter
    dispatcher = ToolDispatcher()
    adapted = SentienceMCPAdapter(
        delegate=dispatcher,
        call_fn=lambda d, name, args: d.call_tool(name, args),
    )

    # Construct the wrapped session — this is the line a real integration
    # would write. Everything else in this script is demo-specific glue.
    wrapped = wrap_mcp_client(
        target=adapted,
        session_manager=session_manager,
        cache=cache,
        sink_writer=sink,
        agent_id="claude-demo-v1",
        agent_version="0.1.0",
        vendor_id="sentience-demo",
        declared_capabilities=["crm.read", "vector_store.write"],
        owner_claim=os.environ.get("USER", "demo-operator"),
        stated_objective="Look up Acme Corp usage and store Q1 snapshot",
        classification_hook=extract_classification,
    )

    # Anthropic client
    client = anthropic.Anthropic(api_key=api_key)

    messages: list[dict] = [
        {"role": "user", "content": TASK_PROMPT},
    ]

    iterations = 0
    final_stop_reason: Optional[str] = None

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
                print(f"error: Claude API error on iteration {iterations}: {exc}", file=sys.stderr)
                return 2

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

            # Append the assistant's response (with tool_use blocks) to
            # the conversation, then build tool_result blocks for each
            # tool the model called.
            messages.append({"role": "assistant", "content": response.content})

            tool_results: list[dict] = []
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
                    raw_result = wrapped.send_tool_call(wrapper_name, block.input)
                    # Strip the _metadata key before sending back to Claude —
                    # the metadata is for the wrapper, not the model.
                    visible_result = (
                        {k: v for k, v in raw_result.items() if k != "_metadata"}
                        if isinstance(raw_result, dict)
                        else raw_result
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(visible_result),
                        }
                    )
                except Exception as exc:
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"tool error: {exc}",
                            "is_error": True,
                        }
                    )

            messages.append({"role": "user", "content": tool_results})

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
    print(f"Session closed after {iterations} iteration(s).")
    return 0


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print(
            "error: ANTHROPIC_API_KEY is required.\n"
            "See https://console.anthropic.com/settings/keys",
            file=sys.stderr,
        )
        return 1

    sink_path = Path(
        os.environ.get("SENTIENCE_DEMO_SINK_PATH", DEFAULT_SINK_PATH)
    ).expanduser()
    model = os.environ.get("SENTIENCE_DEMO_MODEL", DEFAULT_MODEL)

    try:
        return asyncio.run(run_demo(api_key=api_key, sink_path=sink_path, model=model))
    except KeyboardInterrupt:
        print("\ninterrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
