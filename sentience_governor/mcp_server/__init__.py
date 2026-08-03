"""Sentience MCP server (v0.3.0).

Exposes Sentience governance analyzers as MCP tools that Claude (and any
MCP-aware harness) can call. stdio transport, opt-in. Distinct from the
`wrapper.mcp` interception proxy: this is the "governance-as-tools" server.

The `mcp` SDK is an optional dependency (`pip install
"sentience-governor[mcp]"`). Import the server module's *_payload functions
freely (they carry no mcp dependency); importing/constructing the server
itself requires the optional package.
"""
