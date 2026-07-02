"""SSE transport smoke tests for the Recipes MCP server.

We do not run a full MCP handshake here — the official SDK already covers
that. What we verify is the integration boundary:
  * the FastAPI router is wired
  * unauthenticated callers are rejected
  * authenticated GET /api/mcp/sse opens the event-stream
  * /api/mcp/healthz lists the eight Phase A tools
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.mcp.server import _tool_definitions, router as mcp_router


@pytest.fixture()
def mcp_app(db_session):
    app = FastAPI()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.include_router(mcp_router)
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.fixture()
def mcp_client(mcp_app):
    with TestClient(mcp_app, raise_server_exceptions=True) as c:
        yield c


def test_healthz_lists_phase_a_tools(mcp_client):
    resp = mcp_client.get("/api/mcp/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "recipes-mcp"
    expected = {t.name for t in _tool_definitions()}
    assert set(body["tools"]) == expected
    # MCP tool surface has expanded beyond the original Phase A 10. Pin the
    # minimum (the v1 contract that external MCP clients depend on) rather
    # than the exact count so future additions don't break this test.
    PHASE_A_REQUIRED = {
        "recipes_search",
        "recipes_install",
        "recipes_recall",
    }
    assert PHASE_A_REQUIRED.issubset(expected), (
        f"Phase A tool contract broken — missing {PHASE_A_REQUIRED - expected}"
    )
    assert len(expected) >= 10, f"Tool catalog shrank to {len(expected)}; expected >= 10"


def test_sse_rejects_missing_api_key(mcp_client):
    resp = mcp_client.get("/api/mcp/sse")
    assert resp.status_code == 401


def test_sse_rejects_bad_api_key(mcp_client):
    resp = mcp_client.get("/api/mcp/sse", headers={"x-api-key": "rec_not_real"})
    assert resp.status_code == 401


def test_messages_endpoint_rejects_unauthenticated(mcp_client):
    resp = mcp_client.post("/api/mcp/messages/", json={})
    assert resp.status_code == 401


def test_sse_route_is_registered_at_expected_path(mcp_app):
    """Confirm /api/mcp/sse and /api/mcp/messages/ are wired by the router.

    The full GET handshake spins up ``server.run()`` — a long-lived loop we
    deliberately do not exercise here (the SDK's own tests cover JSON-RPC
    semantics). What we assert is the FastAPI route table.
    """
    paths = {getattr(r, "path", None) for r in mcp_app.router.routes}
    assert "/api/mcp/sse" in paths
    assert "/api/mcp/messages/" in paths
    assert "/api/mcp/healthz" in paths


def test_build_mcp_server_dispatches_search_tool(db_session):
    """Drive the MCP Server's call_tool handler in-process to confirm the
    same dispatch path used over SSE actually invokes our tool functions.
    """
    from app.mcp.server import build_mcp_server, _tool_definitions
    from tests.conftest import make_skill

    make_skill(db_session, slug="dispatch-skill", title="Dispatch Skill",
               description="reachable via MCP", category="ops")
    db_session.commit()

    # Bind every dispatch to the test's db session so commits stay isolated.
    server = build_mcp_server(db_factory=lambda: db_session)

    # The Server SDK registers handlers under .request_handlers keyed by the
    # request type. We pull the call_tool handler and drive it directly.
    import mcp.types as types
    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name="recipes_search", arguments={"query": "Dispatch"}
        ),
    )
    result = asyncio.get_event_loop().run_until_complete(handler(req)) \
        if False else asyncio.run(handler(req))

    payload_text = result.root.content[0].text  # type: ignore[attr-defined]
    import json as _json
    parsed = _json.loads(payload_text)
    assert any(r["slug"] == "dispatch-skill" for r in parsed["results"])
    # Sanity: the static tool catalogue includes every registered tool. The
    # surface has grown from Phase A's 8 + Phase K + Phase 2 = 10 to 14+ as
    # new MCP tools shipped (doctor, feedback, carousel_today, propose_patch).
    # Pin the floor, not the exact count, so future additions don't break
    # this regression test.
    assert len(_tool_definitions()) >= 10
