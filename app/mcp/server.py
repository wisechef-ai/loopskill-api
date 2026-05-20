"""Recipes MCP server — triple transport (SSE, StreamableHTTP, stdio).

* SSE/HTTP — mounted at ``/api/mcp/sse`` (event-stream) +
  ``/api/mcp/messages/`` (POSTs from the client).
* StreamableHTTP — mounted at ``/api/mcp/http`` (single-endpoint POST,
  stateful sessions, MCP spec 2025-03-26).
* stdio   — ``python -m app.mcp`` for Claude Desktop and other local clients.

Auth on the SSE/StreamableHTTP side reuses ``app.middleware``'s validator.
The stdio side trusts the env (``RECIPES_API_KEY``) since stdio is a local
trust boundary.  The handler dispatches a static tool catalogue to the nine
Phase A + Phase K tools.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import mcp.types as types
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.config import settings
from app.database import SessionLocal, get_db
from app.mcp.auth import validate_key
from app.mcp.cookbook_status import get_cookbook_status, invalidate_cookbook_status
from app.mcp.tools import (
    recipes_carousel_today,
    recipes_doctor,
    recipes_feedback,
    recipes_fleet_create,
    recipes_fleet_list,
    recipes_fleet_subscribe,
    recipes_fleet_sync,
    recipes_install,
    recipes_list_cookbook,
    recipes_propose_skill_patch,
    recipes_recall,
    recipes_recipify,
    recipes_report_skill_error,
    recipes_request_recipe,
    recipes_search,
    recipes_seeker,
    recipes_subrecipe_resolve,
    recipes_sync,
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
                        "items": {"type": "string", "enum": ["free", "cook", "operator", "pro", "pro_plus"]},
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
        types.Tool(
            name="recipes_sync",
            description=(
                "Synchronise a cookbook's skills to their latest published "
                "versions. By default (dry_run=false) this APplies updates "
                "immediately. Pass dry_run=true to preview the diff without "
                "mutating state."
            ),
            inputSchema={
                "type": "object",
                "required": ["cookbook_id"],
                "properties": {
                    "cookbook_id": {
                        "type": "string",
                        "description": "UUID of the cookbook to synchronise.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, return the diff without applying changes. "
                            "Default is false (apply immediately)."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_feedback",
            description=(
                "Send feedback about recipes.wisechef.ai. Use when the "
                "user says 'write feedback that...', 'give feedback...', "
                "'report that...', or expresses frustration with the platform "
                "UX, search, billing, or docs. Auto-creates a labelled GitHub "
                "issue. Rate limited per 24h."
            ),
            inputSchema={
                "type": "object",
                "required": ["category", "message"],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["ux", "search", "billing", "docs", "install", "other"],
                    },
                    "message": {"type": "string"},
                    "context": {"type": "object"},
                    "agent_id": {"type": "string"},
                    "force": {"type": "boolean", "default": False},
                    "confirmation": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="recipes_request_recipe",
            description=(
                "Request a new recipe (skill). Use when the user says "
                "'recipify X', 'please add X to recipes', "
                "'we need a recipe for X'. Creates a GitHub wishlist issue."
            ),
            inputSchema={
                "type": "object",
                "required": ["target_name", "why_useful"],
                "properties": {
                    "target_name": {"type": "string"},
                    "why_useful": {"type": "string"},
                    "suggested_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "agent_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="recipes_report_skill_error",
            description=(
                "Report that an installed recipe is broken, has wrong "
                "instructions, or fails on this host. Use when the user says "
                "'this skill is broken', 'report this skill', or when an "
                "install/run fails. Auto-creates a labelled GitHub issue with "
                "the failure signature."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "signature", "summary"],
                "properties": {
                    "slug": {"type": "string"},
                    "signature": {"type": "string"},
                    "summary": {"type": "string"},
                    "details": {"type": "string"},
                    "agent_id": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="recipes_propose_skill_patch",
            description=(
                "Submit a working patch (draft PR) to a recipes-marketplace skill "
                "on wisechef-ai/recipes-api. Use when you have ALREADY fixed a skill "
                "locally during install or use and want to ship the fix back so other "
                "agents do not hit the same bug. Allowed file paths: SKILL.md, "
                "references/*.md, templates/*.{yml,yaml,sh,env,md}. Script changes "
                "(scripts/*, install.sh, recipe.yaml) are NOT allowed here — describe "
                "those as a comment on the skill-error issue body instead. Hard limits: "
                "3 files max, 200 lines per file, 600 lines total. Rate limited to "
                "1 patch per 24h per (agent, skill). Returns dedup_hash and (eventually) pr_url."
            ),
            inputSchema={
                "type": "object",
                "required": ["slug", "base_version", "files", "rationale"],
                "properties": {
                    "slug": {"type": "string"},
                    "base_version": {"type": "string"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["path", "content"],
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                    },
                    "rationale": {"type": "string"},
                    "evidence_install_id": {"type": "string"},
                    "agent_id_anon": {"type": "string"},
                },
            },
        ),
        # ── Phase E: fleet tools ─────────────────────────────────────────────
        types.Tool(
            name="recipes_fleet_create",
            description=(
                "Create a named fleet of agents. Returns a one-time fleet API key "
                "(rec_fleet_*) for x-fleet-key authentication. The key is shown ONCE."
            ),
            inputSchema={
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        ),
        types.Tool(
            name="recipes_fleet_subscribe",
            description=(
                "Subscribe a cookbook to a fleet on a given channel " "(stable, canary, frozen). Idempotent."
            ),
            inputSchema={
                "type": "object",
                "required": ["fleet_id", "cookbook_id"],
                "properties": {
                    "fleet_id": {"type": "string"},
                    "cookbook_id": {"type": "string"},
                    "channel": {
                        "type": "string",
                        "enum": ["stable", "canary", "frozen"],
                        "default": "stable",
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_fleet_sync",
            description=(
                "Synchronise all cookbooks subscribed to the fleet. Aggregates "
                "per-cookbook sync results. Pass dry_run=true to preview."
            ),
            inputSchema={
                "type": "object",
                "required": ["fleet_id"],
                "properties": {
                    "fleet_id": {"type": "string"},
                    "dry_run": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, preview changes without applying.",
                    },
                },
            },
        ),
        types.Tool(
            name="recipes_fleet_list",
            description="List all fleets owned by the caller with their cookbook subscriptions.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


ToolDispatch = Callable[[Session, dict[str, Any], dict[str, Any]], Awaitable[Any] | Any]


def _ctx_from_caller(caller: dict[str, Any]) -> AuthContext:
    """Extract or reconstruct an AuthContext from a caller dict.

    Phase B: the AuthContext is the canonical auth object. The caller dict
    is kept for backwards compat with older code paths. If the validate_key
    result already contains an 'auth_ctx' key we use it directly; otherwise
    we reconstruct from the dict fields.
    """
    ctx = caller.get("auth_ctx")
    if isinstance(ctx, AuthContext):
        return ctx
    # Reconstruct for legacy caller dicts (stdio fallback, old tests)
    scope = caller.get("scope", "master")
    # Remap legacy 'operator' scope to 'master' for stdio/fallback paths
    if scope == "operator":
        scope = "master"
    return AuthContext(
        scope=scope,  # type: ignore[arg-type]
        user_id=caller.get("user_id"),
        api_key_id=caller.get("api_key_id"),
    )


def _dispatch(name: str, db: Session, args: dict[str, Any], caller: dict[str, Any]) -> Any:
    """Route a tool name to its implementation. Pure sync — no I/O outside the DB."""
    # Phase B (Issue #5/#6/#7/#15): resolve AuthContext from caller.
    ctx = _ctx_from_caller(caller)

    if name == "recipes_search":
        return recipes_search(
            db,
            query=args.get("query"),
            category=args.get("category"),
            tier=args.get("tier"),
            limit=int(args.get("limit", 20)),
        )
    if name == "recipes_install":
        return recipes_install(
            db,
            slug=args["slug"],
            api_key_id=caller.get("api_key_id"),
            ctx=ctx,
        )
    if name == "recipes_list_cookbook":
        return recipes_list_cookbook(
            db,
            user_id=caller.get("user_id"),
            cookbook_id=args.get("cookbook_id"),
        )
    if name == "recipes_recall":
        return recipes_recall(db, **args)
    if name == "recipes_recipify":
        return recipes_recipify(db, ctx=ctx, **args)
    if name == "recipes_carousel_today":
        return recipes_carousel_today(db)
    if name == "recipes_subrecipe_resolve":
        return recipes_subrecipe_resolve(db, **args)
    if name == "recipes_doctor":
        return recipes_doctor(db, install_dir=args["install_dir"])
    if name == "recipes_seeker":
        return recipes_seeker(db, **args)
    if name == "recipes_sync":
        return recipes_sync(
            db,
            cookbook_id=args["cookbook_id"],
            dry_run=args.get("dry_run", False),
            ctx=ctx,
        )
    if name == "recipes_feedback":
        return recipes_feedback(
            db,
            category=args["category"],
            message=args["message"],
            context=args.get("context"),
            agent_id=args.get("agent_id"),
            force=args.get("force", False),
            confirmation=args.get("confirmation"),
            api_key_id=caller.get("api_key_id"),
        )
    if name == "recipes_request_recipe":
        return recipes_request_recipe(
            db,
            target_name=args["target_name"],
            why_useful=args["why_useful"],
            suggested_sources=args.get("suggested_sources"),
            agent_id=args.get("agent_id"),
            api_key_id=caller.get("api_key_id"),
        )
    if name == "recipes_report_skill_error":
        return recipes_report_skill_error(
            db,
            slug=args["slug"],
            signature=args["signature"],
            summary=args["summary"],
            details=args.get("details"),
            agent_id=args.get("agent_id"),
            api_key_id=caller.get("api_key_id"),
        )
    if name == "recipes_propose_skill_patch":
        return recipes_propose_skill_patch(
            db,
            slug=args["slug"],
            base_version=args["base_version"],
            files=args["files"],
            rationale=args["rationale"],
            evidence_install_id=args.get("evidence_install_id"),
            agent_id_anon=args.get("agent_id_anon"),
            api_key_id=caller.get("api_key_id"),
        )
    # Phase E: fleet tools
    if name == "recipes_fleet_create":
        return recipes_fleet_create(
            db,
            name=args["name"],
            ctx=ctx,
        )
    if name == "recipes_fleet_subscribe":
        return recipes_fleet_subscribe(
            db,
            fleet_id=args["fleet_id"],
            cookbook_id=args["cookbook_id"],
            channel=args.get("channel", "stable"),
            ctx=ctx,
        )
    if name == "recipes_fleet_sync":
        return recipes_fleet_sync(
            db,
            fleet_id=args["fleet_id"],
            dry_run=args.get("dry_run", False),
            ctx=ctx,
        )
    if name == "recipes_fleet_list":
        return recipes_fleet_list(
            db,
            ctx=ctx,
        )
    raise ValueError(f"unknown tool: {name}")


def call_tool_sync(
    name: str,
    args: dict[str, Any],
    *,
    caller: dict[str, Any] | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Direct synchronous entry-point used by tests and the stdio loop.

    Injects a ``cookbook_status`` block when the caller is an authenticated
    user with outdated skills in their cookbooks.
    """
    caller = caller or {"scope": "operator", "user_id": None}
    own_db = db is None
    session = db or SessionLocal()
    try:
        payload = _dispatch(name, session, args or {}, caller)

        # After a successful recipes_sync apply, invalidate cached status
        if name == "recipes_sync" and isinstance(payload, dict) and payload.get("applied"):
            invalidate_cookbook_status(caller.get("user_id"))

        # Inject cookbook_status for authenticated users (skip for recipes_sync
        # itself to avoid noisy double-reporting — sync already returns the diff).
        if isinstance(payload, dict) and name != "recipes_sync":
            user_id = caller.get("user_id")
            status = get_cookbook_status(session, user_id)
            if status:
                payload["cookbook_status"] = status

        return payload
    finally:
        if own_db:
            session.close()


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

    Falls back to the stdio operator default when there is no active
    request context (stdio loop, direct call_tool_sync, in-process tests
    that drive the request handler manually).
    """
    fallback = {"scope": "operator", "user_id": None, "api_key_id": None}
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


def build_mcp_server(db_factory: Callable[[], Session] = SessionLocal) -> Server:
    """Build a fresh ``mcp.Server`` instance bound to the supplied db factory.

    A factory (rather than a single session) is required because each tool
    invocation needs an independent session — long-lived MCP connections
    would otherwise leak transactions.

    The server is built once per transport connection (SSE: per ``GET /sse``;
    StreamableHTTP: once at app startup, reused across sessions; stdio: once
    per process). Auth context is therefore *not* baked into the closure —
    it is resolved per-call from the active ``request_context`` so each
    JSON-RPC ``tools/call`` runs with the user that authenticated *that*
    call. See ``_caller_from_request_context``.
    """
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:  # pragma: no cover - thin shim
        return _tool_definitions()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        caller = _caller_from_request_context(server)
        db = db_factory()
        try:
            payload = _dispatch(name, db, arguments or {}, caller)
        except Exception as exc:  # noqa: BLE001
            payload = {"error": str(exc), "tool": name}
        finally:
            db.close()
        return [types.TextContent(type="text", text=json.dumps(payload, default=str))]

    return server


# ── FastAPI router (SSE transport) ──────────────────────────────────────────
#
# Public surface: /api/mcp/{healthz,sse,messages/}
#
# Why /api/mcp instead of plain /mcp:
# The Cloudflare zone fronting recipes.wisechef.ai intercepts literal /mcp/*
# paths at the edge (likely CF's managed AI Gateway / Workers MCP product)
# and returns 404 before the request ever reaches our cloudflared tunnel.
# /api/mcp/* passes through cleanly via the existing /api/* tunnel rule.
# Verified 2026-05-07 by inspecting cloudflared_tunnel_total_requests counter.

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# The SseServerTransport must be shared between GET /sse and POST /messages.
# The path passed here is the public path the SSE endpoint advertises to the
# client for follow-up POSTs, so it must match the POST route below.
_sse_transport = SseServerTransport("/api/mcp/messages/")

# ── StreamableHTTP session manager ─────────────────────────────────────────
#
# The StreamableHTTPSessionManager wraps StreamableHTTPServerTransport and
# handles session creation/cleanup automatically.  It needs a task group
# (started in ``run()``) to manage concurrent sessions.
#
# session_idle_timeout=1800s (30 min) prevents Cloudflare's 100s streaming
# timeout from firing on long-running tools by keeping the session alive.
# NOTE: The MCP SDK 1.27 does not expose a ping_interval_seconds parameter;
# if one appears in a future release, add it here and reduce idle_timeout.

_http_session_manager: StreamableHTTPSessionManager | None = None


def get_http_session_manager() -> StreamableHTTPSessionManager:
    """Lazy-initialise the StreamableHTTP session manager.

    Must be called at app startup (inside the lifespan) so the task group
    is available.  The session manager reuses ``build_mcp_server()`` — the
    same factory as SSE and stdio — so tool definitions are never duplicated.
    """
    global _http_session_manager
    if _http_session_manager is None:
        _http_session_manager = StreamableHTTPSessionManager(
            app=build_mcp_server(),
            json_response=False,
            stateless=False,
            session_idle_timeout=1800,  # 30 min — prevents CF 100s timeout
        )
    return _http_session_manager


def _reset_http_session_manager() -> None:
    """Reset the global session manager (for tests only)."""
    global _http_session_manager
    _http_session_manager = None


def _authenticate(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Validate x-api-key on SSE handshake. Raises HTTPException on failure.

    Uses ``Depends(get_db)`` rather than ``SessionLocal()`` directly so the
    test suite's ``dependency_overrides`` substitution still applies.

    On success, stashes the resolved caller dict on ``request.state.mcp_caller``
    (backed by ``scope["state"]``). The MCP transport later builds a fresh
    Starlette ``Request`` from the same scope when it dispatches each
    JSON-RPC message; that Request reads the same state, so tool handlers
    can pull the authenticated caller via ``server.request_context``. See
    ``_caller_from_request_context`` for the consumer side.
    """
    key = request.headers.get("x-api-key")
    result = validate_key(key, db)
    # Reject both an unrecognised key ("unauthorized") AND a missing key
    # ("anonymous"). The SSE / messages transports require an authenticated
    # caller — anonymous is not a valid scope here. Previously only
    # "unauthorized" was rejected, so a MISSING x-api-key fell through this
    # gate into the long-lived server.run() loop instead of returning 401.
    # In production the global APIKeyMiddleware masks this, but the router's
    # own defense-in-depth gate must stand on its own. (Caught by the hung
    # tests/test_mcp_server.py::test_sse_rejects_missing_api_key.)
    if result["scope"] in ("unauthorized", "anonymous"):
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key header")
    request.state.mcp_caller = result
    # Phase B (Issue #5): stamp auth_ctx — identical schema to REST path
    auth_ctx = result.get("auth_ctx")
    if auth_ctx is None:
        auth_ctx = AuthContext.anonymous()
    request.state.auth_ctx = auth_ctx
    return result


@router.get("/healthz")
def mcp_healthz() -> dict[str, Any]:
    """Return MCP server health info including registered tool names."""
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
    ``/api/mcp/messages/`` for actual JSON-RPC traffic.
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
        await _sse_transport.handle_post_message(request.scope, request.receive, request._send)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp message dispatch failed: %s", exc)
        return JSONResponse({"detail": "bad message"}, status_code=400)
    return Response(status_code=202)


# ── StreamableHTTP transport route ──────────────────────────────────────────

# StreamableHTTP uses a raw ASGI handler (not a FastAPI route) because the
# session manager sends HTTP responses directly via the ASGI ``send``
# callable — FastAPI's route wrapper would attempt to send a second response
# and trigger "Received multiple http.response.start messages".
#
# We use a Starlette Mount to attach it at /api/mcp/http.

from starlette.routing import Mount


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
            # exactly the operator-scope fallback contract.
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

    server = build_mcp_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )
