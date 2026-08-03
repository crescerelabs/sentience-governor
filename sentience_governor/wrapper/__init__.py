"""Wrapper — MCP abstraction (Path A) and LangChain integration (Path B)."""

from sentience_governor.wrapper.mcp import MCPClientLike, SentienceMCPAdapter, wrap_mcp_client
from sentience_governor.wrapper.langchain_adapter import (
    SentienceCallbackHandler,
    SentienceMiddleware,
)

__all__ = [
    "MCPClientLike",
    "SentienceMCPAdapter",
    "wrap_mcp_client",
    "SentienceCallbackHandler",
    "SentienceMiddleware",
]
