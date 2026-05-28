"""AuthContext propagation for MCP SSE/StreamableHTTP transports.

Contains _caller_from_request_context which extracts the authenticated
caller dict from the active MCP request context.

Do NOT modify app/mcp/auth.py — that module handles key validation.
This module only threads the result of that validation into tool calls.
"""

from __future__ import annotations

from typing import Any

from mcp.server.lowlevel import Server


def _caller_from_request_context(server: Server) -> dict[str, Any]:
    """Return the caller dict stashed on the active request, or a stdio fallback.

    SSE and StreamableHTTP transports both attach the original Starlette
    ``Request`` object to the MCP ``RequestContext.request`` field. Our auth
    layer (``_authenticate`` for SSE/messages, the ASGI wrapper for
    StreamableHTTP) has already validated the x-api-key and stashed the
    resolved caller dict on ``request.state.mcp_caller`` (which is backed by
    ``scope["state"]``). We retrieve it here so each tool call sees the
    caller that actually authenticated *this* call — not a hardcoded
    ``user_id=None`` master.

    Falls back to the stdio master default when there is no active
    request context (stdio loop, direct call_tool_sync, in-process tests
    that drive the request handler manually). Legacy alias — pre-Phase-5.
    """
    fallback = {"scope": "operator", "user_id": None, "api_key_id": None}  # legacy alias (pre-Phase-5)
    try:
        ctx = server.request_context
    except LookupError:
        return fallback

    request = getattr(ctx, "request", None)
    if request is None:
        return fallback

    # request.state is backed by scope["state"]; if our auth layer didn't run
    # (shouldn't happen — auth gates both transports), fall through cleanly.
    state = getattr(request, "state", None)
    if state is None:
        return fallback
    caller = getattr(state, "mcp_caller", None)
    if not isinstance(caller, dict):
        return fallback
    return caller
