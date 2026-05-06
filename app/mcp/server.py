"""Recipes MCP server — dual transport.

* stdio   — ``python -m app.mcp`` for Claude Desktop and other local clients.
* SSE/HTTP — mounted on the FastAPI app at ``/mcp/sse`` (event-stream) and
  ``/mcp/messages/`` (POSTs from the client).

Auth on the SSE side reuses ``app.middleware``'s validator. The stdio side
trusts the env (``RECIPES_API_KEY``) since stdio is a local trust boundary.
The handler dispatches a static tool catalogue to the eight Phase A tools.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

import mcp.types as types
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.mcp.auth import validate_key
from app.mcp.tools import (
    recipes_carousel_today,
    recipes_doctor,
    recipes_install,
    recipes_list_cookbook,
    recipes_recall,
    recipes_recipify,
    recipes_search,
    recipes_seeker,
    recipes_subrecipe_resolve,
)

logger = logging.getLogger("wiserecipes.mcp")

SERVER_NAME = "recipes-mcp"
SERVER_VERSION = "0.1.0"


def _tool_definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="recipes_search",
            description="Full-text search across the public skill catalog.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string"},
                    "tier": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
        ),
        types.Tool(
            name="recipes_install",
            description="Return a signed tarball URL + manifest for a skill slug.",
            inputSchema={
                "type": "object",
                "required": ["slug"],
                "properties": {"slug": {"type": "string"}},
            },
        ),
        types.Tool(
            name="recipes_list_cookbook",
            description="List the caller's cookbook and its skill provenance rows.",
            inputSchema={
                "type": "object",
                "properties": {"cookbook_id": {"type": "string"}},
            },
        ),
        types.Tool(
            name="recipes_recall",
            description="Hybrid (vector + BM25) skill recall ranked for the caller's tier.",
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "local_context_summary": {"type": "string"},
                    "tier_filter": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["free", "cook", "operator"]},
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                },
            },
        ),
        types.Tool(
            name="recipes_recipify",
            description=(
                "Convert a SKILL.md draft into a CookbookSkill row: validates "
                "YAML frontmatter, classifies the category, infers related "
                "skills via embedding cosine, writes the skill to the caller's "
                "cookbook."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "content"],
                "properties": {
                    "slug": {"type": "string"},
                    "content": {"type": "string"},
                    "target_cookbook_id": {"type": "string"},
                    "visibility": {
                        "type": "string",
                        "enum": ["private", "public_pending_review"],
                        "default": "private",
                    },
                    "target_subrecipe_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="recipes_carousel_today",
            description="Today's curated carousel of skills.",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="recipes_subrecipe_resolve",
            description="Phase C stub — resolve a sub-recipe key to a scope.",
            inputSchema={"type": "object"},
        ),
        types.Tool(
            name="recipes_doctor",
            description="Audit a local skill install directory for missing files and hardcoded paths.",
            inputSchema={
                "type": "object",
                "required": ["install_dir"],
                "properties": {"install_dir": {"type": "string"}},
            },
        ),
        types.Tool(
            name="recipes_seeker",
            description=(
                "Probe local vendor skill directories (Claude / Codex / "
                "Hermes / OpenCode) and diff against the public catalog. "
                "READ-ONLY — never mutates vendor dirs."
            ),
            inputSchema={"type": "object"},
        ),
    ]


ToolDispatch = Callable[[Session, dict[str, Any], dict[str, Any]], Awaitable[Any] | Any]


def _dispatch(name: str, db: Session, args: dict[str, Any], caller: dict[str, Any]) -> Any:
    """Route a tool name to its implementation. Pure sync — no I/O outside the DB."""
    if name == "recipes_search":
        return recipes_search(
            db,
            query=args.get("query"),
            category=args.get("category"),
            tier=args.get("tier"),
            limit=int(args.get("limit", 20)),
        )
    if name == "recipes_install":
        return recipes_install(db, slug=args["slug"], api_key_id=caller.get("api_key_id"))
    if name == "recipes_list_cookbook":
        return recipes_list_cookbook(
            db,
            user_id=caller.get("user_id"),
            cookbook_id=args.get("cookbook_id"),
        )
    if name == "recipes_recall":
        return recipes_recall(db, **args)
    if name == "recipes_recipify":
        return recipes_recipify(db, **args)
    if name == "recipes_carousel_today":
        return recipes_carousel_today(db)
    if name == "recipes_subrecipe_resolve":
        return recipes_subrecipe_resolve(db, **args)
    if name == "recipes_doctor":
        return recipes_doctor(db, install_dir=args["install_dir"])
    if name == "recipes_seeker":
        return recipes_seeker(db, **args)
    raise ValueError(f"unknown tool: {name}")


def call_tool_sync(
    name: str,
    args: dict[str, Any],
    *,
    caller: dict[str, Any] | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Direct synchronous entry-point used by tests and the stdio loop."""
    caller = caller or {"scope": "operator", "user_id": None}
    own_db = db is None
    session = db or SessionLocal()
    try:
        return _dispatch(name, session, args or {}, caller)
    finally:
        if own_db:
            session.close()


def build_mcp_server(db_factory: Callable[[], Session] = SessionLocal) -> Server:
    """Build a fresh ``mcp.Server`` instance bound to the supplied db factory.

    A factory (rather than a single session) is required because each tool
    invocation needs an independent session — long-lived MCP connections
    would otherwise leak transactions.
    """
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:  # pragma: no cover - thin shim
        return _tool_definitions()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        db = db_factory()
        try:
            payload = _dispatch(name, db, arguments or {}, {"scope": "operator", "user_id": None})
        except Exception as exc:  # noqa: BLE001
            payload = {"error": str(exc), "tool": name}
        finally:
            db.close()
        return [types.TextContent(type="text", text=json.dumps(payload, default=str))]

    return server


# ── FastAPI router (SSE transport) ──────────────────────────────────────────

router = APIRouter(prefix="/mcp", tags=["mcp"])

# The SseServerTransport must be shared between GET /sse and POST /messages.
_sse_transport = SseServerTransport("/mcp/messages/")


def _authenticate(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Validate x-api-key on SSE handshake. Raises HTTPException on failure.

    Uses ``Depends(get_db)`` rather than ``SessionLocal()`` directly so the
    test suite's ``dependency_overrides`` substitution still applies.
    """
    key = request.headers.get("x-api-key")
    result = validate_key(key, db)
    if result["scope"] == "unauthorized":
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key header")
    return result


@router.get("/healthz")
def mcp_healthz() -> dict[str, Any]:
    return {
        "name": SERVER_NAME,
        "version": SERVER_VERSION,
        "tools": [t.name for t in _tool_definitions()],
    }


@router.get("/sse")
async def mcp_sse(
    request: Request,
    _auth: dict[str, Any] = Depends(_authenticate),
):
    """SSE transport endpoint. Long-lived connection — client posts to
    ``/mcp/messages/`` for actual JSON-RPC traffic.
    """
    server = build_mcp_server()

    async with AsyncExitStack() as stack:
        streams = await stack.enter_async_context(
            _sse_transport.connect_sse(request.scope, request.receive, request._send)
        )
        read_stream, write_stream = streams
        init_options = server.create_initialization_options()
        await server.run(read_stream, write_stream, init_options)
    return Response(status_code=204)


@router.post("/messages/")
async def mcp_messages(
    request: Request,
    _auth: dict[str, Any] = Depends(_authenticate),
):
    """POST endpoint paired with the SSE channel. Auth re-checked here so
    a stale session-id from another caller can't piggyback."""
    try:
        await _sse_transport.handle_post_message(
            request.scope, request.receive, request._send
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp message dispatch failed: %s", exc)
        return JSONResponse({"detail": "bad message"}, status_code=400)
    return Response(status_code=202)


# ── stdio entry point ──────────────────────────────────────────────────────

async def run_stdio() -> None:  # pragma: no cover - exercised via __main__
    """Run the MCP server on stdio (for Claude Desktop & similar)."""
    expected = os.environ.get("RECIPES_API_KEY") or settings.API_KEY
    provided = os.environ.get("RECIPES_API_KEY")
    if provided and provided != expected and provided != settings.API_KEY:
        logger.warning("RECIPES_API_KEY mismatch — accepting anyway in stdio trust mode")

    from mcp.server.stdio import stdio_server

    server = build_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
