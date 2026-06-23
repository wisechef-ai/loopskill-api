"""MCP dispatch shim — re-exports from app.mcp.server for backward compat.

The actual _dispatch, call_tool_sync, _ctx_from_caller, and ToolDispatch
implementations live in server.py so that:
  - patch.object(server_mod, "recipes_sync", ...) is visible to _dispatch
  - monkeypatch.setattr(server_mod, "_dispatch", ...) still intercepts calls

For organizational reference only: see app/mcp/server.py for the implementations.
"""

from app.mcp.server import (  # noqa: F401
    ToolDispatch,
    _ctx_from_caller,
    _dispatch,
    call_tool_sync,
)

__all__ = ["ToolDispatch", "_ctx_from_caller", "_dispatch", "call_tool_sync"]
