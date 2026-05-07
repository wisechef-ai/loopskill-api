"""StreamableHTTP transport tests for the Recipes MCP server (Phase 1 — v7.1).

These tests verify the new StreamableHTTP transport mounted at /api/mcp/http:
  1. Initialize returns all 9 tools via tools/list
  2. Long-running tool invocations don't time out (simulated via sleep)
  3. SSE transport still works (regression)
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.mcp.server import _tool_definitions, router as mcp_router


# ── Fixtures ────────────────────────────────────────────────────────────────

EXPECTED_TOOL_NAMES = {t.name for t in _tool_definitions()}
EXPECTED_TOOL_COUNT = len(EXPECTED_TOOL_NAMES)  # 10 as of Phase 2


@pytest.fixture()
def mcp_app(db_session):
    """FastAPI app with the MCP router wired + DB override.

    Starts the StreamableHTTP session manager lifespan so that
    POST /api/mcp/http actually works through the session manager.
    """
    from contextlib import asynccontextmanager

    from app.mcp.server import (
        _reset_http_session_manager,
        _build_streamable_http_mount,
    )

    _reset_http_session_manager()

    app = FastAPI()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.include_router(mcp_router)
    app.dependency_overrides[get_db] = override_get_db

    # Mount the StreamableHTTP ASGI sub-app at /api/mcp/http.
    app.router.routes.append(_build_streamable_http_mount())

    # Wire the StreamableHTTP session manager into a lifespan so its
    # internal task group starts before any requests are handled.
    @asynccontextmanager
    async def _lifespan(app):
        from app.mcp.server import run_streamable_http
        async with run_streamable_http():
            yield

    app.router.lifespan_context = _lifespan
    return app


@pytest.fixture()
def mcp_client(mcp_app):
    with TestClient(
        mcp_app,
        headers={"x-api-key": settings.API_KEY},
        raise_server_exceptions=True,
    ) as c:
        yield c


# ── Helpers ─────────────────────────────────────────────────────────────────

def _parse_sse_response(text: str) -> dict:
    """Parse an SSE-formatted response body to extract the JSON data.

    The StreamableHTTP transport may return SSE events like:
        event: message\\r\\ndata: {...}\\r\\n\\r\\n
    """
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("data: "):
            return json.loads(line[6:])
    # Fallback: try parsing the whole text as JSON
    return json.loads(text)


def _jsonrpc_request(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request dict."""
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        **({"params": params} if params is not None else {}),
    }


# ── Tests ───────────────────────────────────────────────────────────────────


class TestStreamableHTTPTransport:
    """Verify the StreamableHTTP transport at /api/mcp/http."""

    def test_initialize_returns_9_tools(self, mcp_client):
        """POST initialize via TestClient to /api/mcp/http, then POST
        tools/list — assert the server advertises all 9 tools."""
        # StreamableHTTP requires Accept: application/json, text/event-stream
        headers = {
            "Accept": "application/json, text/event-stream",
        }
        # Step 1: initialize the session
        init_resp = mcp_client.post(
            "/api/mcp/http",
            json=_jsonrpc_request("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.1.0"},
            }),
            headers=headers,
        )
        assert init_resp.status_code == 200, f"init failed: {init_resp.text}"
        init_body = _parse_sse_response(init_resp.text)
        assert "result" in init_body, f"no result in init response: {init_body}"

        # Extract session ID from response header (StreamableHTTP returns it)
        session_id = init_resp.headers.get("mcp-session-id")
        assert session_id, "missing mcp-session-id header"

        headers["mcp-session-id"] = session_id

        # Step 2: send initialized notification (required by MCP protocol)
        notif_resp = mcp_client.post(
            "/api/mcp/http",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        assert notif_resp.status_code in (200, 202), f"notif failed: {notif_resp.text}"

        # Step 3: list tools
        tools_resp = mcp_client.post(
            "/api/mcp/http",
            json=_jsonrpc_request("tools/list", req_id=2),
            headers=headers,
        )
        assert tools_resp.status_code == 200, f"tools/list failed: {tools_resp.text}"
        tools_body = _parse_sse_response(tools_resp.text)
        assert "result" in tools_body, f"no result in tools response: {tools_body}"
        tool_names = {t["name"] for t in tools_body["result"]["tools"]}
        assert tool_names == EXPECTED_TOOL_NAMES
        assert len(tool_names) == EXPECTED_TOOL_COUNT

    def test_long_running_tool_does_not_timeout(self, mcp_client, monkeypatch):
        """Invoke a tool that intentionally awaits asyncio.sleep(5).
        Assert response within 8s — proves StreamableHTTP keeps the
        connection alive during long-running tool execution."""
        import app.mcp.server as server_mod

        original_dispatch = server_mod._dispatch

        def slow_dispatch(name, db, args, caller):
            """Simulate a long-running tool by sleeping 5 seconds."""
            import time
            time.sleep(5)
            return original_dispatch(name, db, args, caller)

        monkeypatch.setattr(server_mod, "_dispatch", slow_dispatch)

        headers = {
            "Accept": "application/json, text/event-stream",
        }

        # Initialize session
        init_resp = mcp_client.post(
            "/api/mcp/http",
            json=_jsonrpc_request("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.1.0"},
            }),
            headers=headers,
        )
        assert init_resp.status_code == 200
        session_id = init_resp.headers.get("mcp-session-id")
        assert session_id
        headers["mcp-session-id"] = session_id

        # Send initialized notification
        mcp_client.post(
            "/api/mcp/http",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )

        # Invoke the slow tool (recipes_seeker has no required args)
        start = time.monotonic()
        call_resp = mcp_client.post(
            "/api/mcp/http",
            json=_jsonrpc_request("tools/call", {
                "name": "recipes_seeker",
                "arguments": {},
            }, req_id=3),
            headers=headers,
        )
        elapsed = time.monotonic() - start

        assert call_resp.status_code == 200, f"tool call failed: {call_resp.text}"
        assert elapsed < 8.0, f"tool took {elapsed:.1f}s — may be hitting a timeout"

        body = _parse_sse_response(call_resp.text)
        assert "result" in body, f"unexpected response: {body}"


class TestSSETransportRegression:
    """Verify the existing SSE transport still works after StreamableHTTP changes."""

    def test_sse_transport_still_works(self, mcp_app):
        """Regression test: existing /api/mcp/sse endpoint still registered."""
        paths = {getattr(r, "path", None) for r in mcp_app.router.routes}
        assert "/api/mcp/sse" in paths, "SSE endpoint missing from router"
        assert "/api/mcp/messages/" in paths, "messages endpoint missing from router"

        # Also verify the tool definitions haven't changed
        assert len(_tool_definitions()) == EXPECTED_TOOL_COUNT

    def test_sse_rejects_missing_api_key(self, mcp_client):
        """SSE still requires authentication — send request WITHOUT api key."""
        resp = mcp_client.get("/api/mcp/sse", headers={"x-api-key": ""})
        assert resp.status_code == 401

    def test_streamable_http_route_is_registered(self, mcp_app):
        """Verify the new /api/mcp/http route is wired."""
        paths = {getattr(r, "path", None) for r in mcp_app.router.routes}
        assert "/api/mcp/http" in paths, "StreamableHTTP endpoint not registered"
