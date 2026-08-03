#!/usr/bin/env python3
"""Sentience LangChain handler demo — live Claude tool-use loop via callbacks.

Runs a real Claude API tool-use loop through a LangChain agent with the
Sentience `SentienceCallbackHandler` attached as a callback. Writes a
governance trace to `/tmp/sentience-langchain-demo.jsonl`.

This demo verifies Fold 1b (the LangChain callback handler path) end
to end. Companion to `claude_demo.py` (fake MCP tools) and
`claude_airtable_demo.py` (real Airtable via MCP wrapper). All three
demos produce the same trace schema; they differ only in which
integration surface the Governor attaches to.

What this demo proves
---------------------
- The LangChain callback handler correctly emits AGENT_REGISTERED,
  INTENT_DECLARED, SCOPE_ASSERTED, and CONTEXT_SNAPSHOT events during
  a real Claude tool-use session.
- `intent_source=inferred` + `intent_confidence=inferred_low` appear
  on the INTENT_DECLARED event — the LangChain path's honest intent
  classification (the integrator did not supply a `stated_objective`;
  the handler extracted it from the chain's invocation input).
- `CONTEXT_UNCLASSIFIED` + `POL-003` fire on every CONTEXT_SNAPSHOT
  event — the LangChain path does not currently support a
  classification hook, so all context arrives unclassified. This is
  honest and expected. See userdocs §3.1 failure-first walkthrough.

What this demo deliberately does NOT do
---------------------------------------
- Does not use real Airtable tools. Airtable connectivity is already
  proven in `claude_airtable_demo.py`. This demo focuses on verifying
  the callback handler's event emission.
- Does not test the MCP wrapper path. That's `claude_demo.py` and
  `claude_airtable_demo.py`.
- Does not verify MEMORY_WRITE_ATTEMPT. The fake tools here do not
  match persistence-target keywords. Memory writes are covered in
  the Airtable demo.

Quick start
-----------
    pip install -e ".[demo]"
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/claude_langchain_demo.py
    sentience-cli /tmp/sentience-langchain-demo.jsonl

Environment overrides
---------------------
    ANTHROPIC_API_KEY            (required)  Claude API credentials
    SENTIENCE_DEMO_SINK_PATH     (default: /tmp/sentience-langchain-demo.jsonl)
    SENTIENCE_DEMO_MODEL         (default: claude-sonnet-4-5)

Exit codes
----------
    0    success
    1    missing ANTHROPIC_API_KEY, or missing langchain-anthropic / langchain-core
    2    Claude / LangChain runtime error
    130  KeyboardInterrupt
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Sentience imports (always present — runtime is the main package)
from sentience_governor.cache.cache import InProcessCache
from sentience_governor.session_manager.manager import SessionManager
from sentience_governor.sink.writer import FileSink, SinkWriter
from sentience_governor.wrapper.langchain_adapter import SentienceCallbackHandler


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SINK_PATH = "/tmp/sentience-langchain-demo.jsonl"
DEFAULT_MODEL = "claude-sonnet-4-5"

TASK_PROMPT = (
    "Look up the customer with ID cust-001, then fetch their Q1 2026 "
    "usage. Once you have both pieces of information, summarise what "
    "you found in one sentence."
)


# ---------------------------------------------------------------------------
# Fake in-process tools (LangChain @tool decorated)
#
# These live inside the demo script — the whole point is to verify the
# Sentience callback handler, not to re-prove external integrations.
# ---------------------------------------------------------------------------

def _build_tools():
    """Build LangChain tools. Imports lazily so a missing dependency
    fails with a clean error at demo-run time, not import time."""
    from langchain_core.tools import tool

    @tool
    def crm_get_customer(id: str) -> dict:
        """Look up a customer record by ID. Returns name, tier, region."""
        if id != "cust-001":
            return {"found": False, "id": id}
        return {
            "found": True,
            "id": "cust-001",
            "name": "Acme Corp",
            "tier": "enterprise",
            "region": "NA",
        }

    @tool
    def crm_fetch_usage(customer_id: str, quarter: str) -> dict:
        """Fetch API call and storage usage for a customer in a given quarter."""
        if customer_id != "cust-001" or quarter != "2026Q1":
            return {
                "found": False,
                "customer_id": customer_id,
                "quarter": quarter,
            }
        return {
            "found": True,
            "customer_id": "cust-001",
            "quarter": "2026Q1",
            "api_calls": 12450,
            "storage_gb": 84.25,
        }

    return [crm_get_customer, crm_fetch_usage]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_demo(api_key: str, sink_path: Path, model: str) -> int:
    """Drive a LangChain agent with SentienceCallbackHandler attached.

    Returns 0 on success, 1 on missing deps, 2 on LangChain/Anthropic error.
    """
    # Lazy imports for the demo extras
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        print(
            "error: langchain-anthropic is not installed.\n"
            "Install demo extras with: pip install -e \".[demo]\"",
            file=sys.stderr,
        )
        return 1

    try:
        from langchain_core.messages import HumanMessage
    except ImportError:
        print(
            "error: langchain-core is not installed.\n"
            "Install demo extras with: pip install -e \".[demo]\"",
            file=sys.stderr,
        )
        return 1

    sink_path.parent.mkdir(parents=True, exist_ok=True)

    # Sentience collaborators
    session_manager = SessionManager()
    cache = InProcessCache()
    sink = SinkWriter(FileSink(str(sink_path)))

    # The callback handler is the Governor's LangChain attachment surface.
    # Note: no stated_objective parameter — the LangChain path does not
    # support integrator-declared intent today. Intent is extracted from
    # the chain's invocation input by the handler itself and classified
    # as intent_source=inferred.
    handler = SentienceCallbackHandler(
        agent_id="claude-langchain-demo-v1",
        session_manager=session_manager,
        cache=cache,
        sink_writer=sink,
        agent_version="0.1.0",
        vendor_id="sentience-demo",
        declared_capabilities=["crm.read"],
        owner_claim=os.environ.get("USER", "demo-operator"),
    )

    # Build the LangChain agent
    tools = _build_tools()
    llm = ChatAnthropic(model=model, api_key=api_key, max_tokens=1024)
    llm_with_tools = llm.bind_tools(tools)

    # Minimal tool-execution loop. LangChain's full agent primitives
    # (AgentExecutor, create_react_agent) add abstraction we don't need
    # to verify the callback handler — a direct bind_tools loop is
    # sufficient and easier to reason about.
    messages = [HumanMessage(content=TASK_PROMPT)]
    tool_map = {t.name: t for t in tools}

    max_iterations = 10
    iterations = 0
    try:
        # The first invoke triggers on_chain_start on the handler. All
        # subsequent tool executions fire on_tool_start / on_tool_end.
        while iterations < max_iterations:
            iterations += 1
            response = llm_with_tools.invoke(
                messages,
                config={"callbacks": [handler]},
            )
            messages.append(response)

            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                # No more tool calls — the model produced a final response
                break

            for tc in tool_calls:
                name = tc["name"]
                args = tc["args"]
                tool_fn = tool_map.get(name)
                if tool_fn is None:
                    result = {"error": f"unknown tool: {name}"}
                else:
                    # Invoke the tool with the handler attached so
                    # on_tool_start / on_tool_end fire and the Governor
                    # emits SCOPE_ASSERTED + CONTEXT_SNAPSHOT events.
                    result = tool_fn.invoke(
                        args,
                        config={"callbacks": [handler]},
                    )
                # JSON-serialize the tool result so it flows back into
                # context as structured data (stable, LLM-friendly,
                # compatible with future classification hooks).
                # default=str handles any non-JSON edge cases defensively.
                from langchain_core.messages import ToolMessage
                messages.append(
                    ToolMessage(
                        content=json.dumps(result, default=str),
                        tool_call_id=tc["id"],
                    )
                )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(
            f"error: LangChain / Anthropic runtime error: {exc}",
            file=sys.stderr,
        )
        return 2

    # Signal the handler that the chain is ending so it can flush any
    # pending state (INTENT_DECLARED if not yet emitted, session close).
    handler.on_chain_end({})

    print()
    print(f"Governance trace written to: {sink_path}")
    print(f"View with: sentience-cli {sink_path}")
    print()
    print(
        "Expected trace characteristics:\n"
        "  - INTENT_DECLARED event has source=inferred, "
        "confidence=inferred_low\n"
        "    (LangChain path extracts intent from chain input; "
        "integrator did not declare\n"
        "    a stated_objective)\n"
        "  - CONTEXT_SNAPSHOT events fire POL-003 "
        "(CONTEXT_UNCLASSIFIED)\n"
        "    (LangChain path does not support a classification hook "
        "today; all context\n"
        "    arrives unclassified — honest and expected)\n"
    )
    return 0


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print(
            "error: ANTHROPIC_API_KEY is required.\n"
            "Get one at https://console.anthropic.com/settings/keys",
            file=sys.stderr,
        )
        return 1

    sink_path = Path(
        os.environ.get("SENTIENCE_DEMO_SINK_PATH", DEFAULT_SINK_PATH)
    ).expanduser()
    model = os.environ.get("SENTIENCE_DEMO_MODEL", DEFAULT_MODEL)

    try:
        return run_demo(api_key=api_key, sink_path=sink_path, model=model)
    except KeyboardInterrupt:
        print("\ninterrupted by user", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
