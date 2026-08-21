"""loopskill-mcp — thin stdio bridge to the hosted LoopSkill MCP server.

Makes ``uvx loopskill-mcp`` a real, runnable local MCP server: it speaks
stdio to the calling agent (Claude Desktop, Hermes, Codex CLI, ...) and
proxies every request to ``https://app.loopskill.io/api/mcp/http/`` — the
hosted StreamableHTTP endpoint documented at
https://app.loopskill.io/.well-known/mcp.json.
"""

from __future__ import annotations

__version__ = "0.9.42"

__all__ = ["__version__"]
