"""Recipes MCP server (Phase A).

Public API surface — phases C/E/G/K extend the tool set defined here.
"""

from app.mcp.server import build_mcp_server, router

__all__ = ["router", "build_mcp_server"]
