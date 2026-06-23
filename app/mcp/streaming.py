"""SSE + StreamableHTTP transport glue for the MCP server.

Contains:
  _sse_transport — shared SseServerTransport instance
  _http_session_manager — global StreamableHTTPSessionManager
  get_http_session_manager / _reset_http_session_manager — lifecycle
  _build_streamable_http_mount — Starlette Mount factory
  run_streamable_http — async context manager for lifespan
  run_stdio — stdio entry-point (Claude Desktop)
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi.responses import JSONResponse
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.routing import Mount

from app.auth_ctx import AuthContext
from app.config import settings

logger = logging.getLogger("wiserecipes.mcp")


_sse_transport = SseServerTransport("/api/mcp/messages/")

_http_session_manager: StreamableHTTPSessionManager | None = None


def get_http_session_manager() -> StreamableHTTPSessionManager:
    """Lazy-initialise the StreamableHTTP session manager.

    Must be called at app startup (inside the lifespan) so the task group
    is available.  The session manager reuses ``build_mcp_server()`` — the
    same factory as SSE and stdio — so tool definitions are never duplicated.
    """
    global _http_session_manager
    if _http_session_manager is None:
        from app.mcp.server import build_mcp_server as _build_server  # late import to avoid circular

        _http_session_manager = StreamableHTTPSessionManager(
            app=_build_server(),
            json_response=False,
            stateless=False,
            session_idle_timeout=1800,  # 30 min — prevents CF 100s timeout
        )
    return _http_session_manager


def _reset_http_session_manager() -> None:
    """Reset the global session manager (for tests only)."""
    global _http_session_manager
    _http_session_manager = None


def _build_streamable_http_mount() -> Mount:
    """Create a Starlette Mount that forwards all requests to the session
    manager's ASGI handler.  Must be called *after* the session manager has
    been initialised (i.e., during app creation, not at import time).

    Includes an auth gate that validates x-api-key on every request.
    """
    mgr = get_http_session_manager()

    async def _asgi_app(scope, receive, send):
        # Auth gate: validate x-api-key before forwarding to the MCP session
        # manager. This mirrors the _authenticate dependency used by the SSE
        # transport routes. On success the caller dict is stashed on
        # scope["state"]["mcp_caller"] so the per-call dispatch (see
        # ``_caller_from_request_context``) can plumb the authenticated
        # user_id / api_key_id into each tool invocation.
        if scope["type"] == "http":
            from app.mcp.auth import validate_key

            request = Request(scope, receive)
            key = request.headers.get("x-api-key")

            # Fast-path: master key without opening a DB session. This avoids
            # needing PostgreSQL in the test environment. The master key has
            # no per-user identity (user_id=None, api_key_id=None) which is
            # exactly the master-scope fallback contract (legacy: was 'operator-scope').
            if not key or not key.startswith("rec_"):
                response = JSONResponse(
                    {"detail": "Invalid or missing x-api-key header"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return

            import hmac as _hmac

            if _hmac.compare_digest(key, settings.API_KEY):
                # Master key — skip DB lookup, stash master caller + auth_ctx.
                master_ctx = AuthContext(scope="master")
                request.state.mcp_caller = {
                    "scope": "master",
                    "user_id": None,
                    "api_key_id": None,
                    "auth_ctx": master_ctx,
                }
                request.state.auth_ctx = master_ctx
            else:
                # Non-master key — need DB lookup
                from app.database import SessionLocal

                db = SessionLocal()
                try:
                    result = validate_key(key, db)
                finally:
                    db.close()
                if result["scope"] == "unauthorized":
                    response = JSONResponse(
                        {"detail": "Invalid or missing x-api-key header"},
                        status_code=401,
                    )
                    await response(scope, receive, send)
                    return
                request.state.mcp_caller = result
                # Phase B (Issue #5): stamp auth_ctx on scope["state"]
                auth_ctx = result.get("auth_ctx")
                if auth_ctx is None:
                    auth_ctx = AuthContext.anonymous()
                request.state.auth_ctx = auth_ctx
        await mgr.handle_request(scope, receive, send)

    return Mount("/api/mcp/http", app=_asgi_app)


@asynccontextmanager
async def run_streamable_http():
    """Async context manager that starts the StreamableHTTP session manager's
    task group.  Call this inside the FastAPI lifespan.

    Usage::

        async with run_streamable_http():
            yield  # app is running
    """
    mgr = get_http_session_manager()
    async with mgr.run():
        yield


# ── stdio entry point ──────────────────────────────────────────────────────


async def run_stdio() -> None:  # pragma: no cover - exercised via __main__
    """Run the MCP server on stdio (for Claude Desktop & similar)."""
    expected = os.environ.get("RECIPES_API_KEY") or settings.API_KEY
    provided = os.environ.get("RECIPES_API_KEY")
    if provided and provided != expected and provided != settings.API_KEY:
        logger.warning("RECIPES_API_KEY mismatch — accepting anyway in stdio trust mode")

    from mcp.server.stdio import stdio_server

    from app.mcp.server import build_mcp_server as _build_server  # late import to avoid circular

    server = _build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
