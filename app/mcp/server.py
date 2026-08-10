"""Recipes MCP server — slim app factory + transport routes.

* SSE/HTTP — mounted at ``/api/mcp/sse`` (event-stream) +
  ``/api/mcp/messages/`` (POSTs from the client).
* StreamableHTTP — mounted at ``/api/mcp/http`` (single-endpoint POST,
  stateful sessions, MCP spec 2025-03-26).
* stdio   — ``python -m app.mcp`` for Claude Desktop and other local clients.

Auth on the SSE/StreamableHTTP side reuses ``app.middleware``'s validator.
The stdio side trusts the env (``RECIPES_API_KEY``) since stdio is a local
trust boundary.  The handler dispatches a static tool catalogue to the nine
Phase A + Phase K tools.

Phase J: the original 1082-line server.py has been split into:
  registry.py      — _tool_definitions() (all tool schemas)
  dispatch.py      — _ctx_from_caller, _dispatch, call_tool_sync
  auth_propagate.py — _caller_from_request_context
  streaming.py     — SSE/HTTP transport globals + _build_streamable_http_mount
  server.py        — this file: build_mcp_server + routes + re-exports

All names that external code imports from app.mcp.server are re-exported here
for backward compatibility.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import mcp.types as types
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.lowlevel import Server
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import SessionLocal, get_db
from app.mcp.auth import validate_key
from app.mcp._alias_map import normalize_tool_name

# Submodule re-exports — backward compat for all existing imports
from app.mcp.registry import _tool_definitions  # noqa: F401

# ── Tool imports (for _dispatch + patch compatibility) ─────────────────────
from app.mcp.bundle_status import (  # noqa: F401  re-exported for existing patch targets
    decorate_with_bundle_status,
    get_bundle_status,
    invalidate_bundle_status,
)
from app.mcp.tools import (  # noqa: F401  loopskill_0622 Phase 8 tools
    loopskill_get_loop,
    loopskill_get_personality,
    loopskill_search_loops,
    loopskill_search_personalities,
)

# activate_0701 Phase A2 — composite loop catalog (NEW MCP names, council §6).
# Individual tool fns live in composite_loop_catalog; dispatch delegates whole-hog.
from app.mcp.tools.composite_loop_catalog import (
    _NOT_HANDLED,  # noqa: F401 — shared sentinel for dispatch delegation
    dispatch_composite_loop as _dispatch_composite_loop,
)
from app.mcp.tools.like import dispatch_library as _dispatch_library
from app.mcp.dispatch_chain import DISPATCH_NOT_HANDLED, run_dispatch_chain
from app.mcp.tools import (
    loopskill_configure_feedback,
    loopskill_doctor,
    loopskill_feedback,
    loopskill_fleet_create,
    loopskill_fleet_list,
    loopskill_fleet_subscribe,
    loopskill_fleet_sync,
    loopskill_fork_list,
    loopskill_install,
    loopskill_bundle_install,
    CookbookInstallError,
    loopskill_install_from_bundle,
    loopskill_pick_best_from_bundle,
    loopskill_compose_bundle_from_links,
    loopskill_list_bundle,
    loopskill_propose_skill_patch,
    loopskill_publish_request,
    loopskill_recall,
    loopskill_skillify,
    loopskill_report_skill_error,
    loopskill_request_skill,
    loopskill_search,
    loopskill_seeker,
    loopskill_share_create,
    loopskill_share_list,
    loopskill_share_revoke,
    loopskill_share_rotate,
    loopskill_subskill_resolve,
    loopskill_sync,
    loopskill_tailor,
    loopskill_tailor_version,
    loopskill_bundle_attach,
    loopskill_bundle_handoff,
    loopskill_connector_publish,
)

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
    # Remap legacy 'operator' scope to 'master' for stdio/fallback paths (legacy alias — pre-Phase-5)
    if scope == "operator":  # legacy alias (pre-Phase-5) → master
        scope = "master"
    return AuthContext(
        scope=scope,  # type: ignore[arg-type]
        user_id=caller.get("user_id"),
        api_key_id=caller.get("api_key_id"),
    )


def _dispatch(name: str, db: Session, args: dict[str, Any], caller: dict[str, Any]) -> Any:
    """Route a tool name to its implementation. Pure sync — no I/O outside the DB.

    lsrename_0713: the recipes_*→loopskill_* back-compat alias layer has been
    dropped. ``normalize_tool_name`` now only resolves the (unrelated) legacy
    ``loop_*`` verifier names from activate_0701 Phase A1. Any ``recipes_*``
    name that reaches this function falls through every branch below and
    hits ``raise ValueError(f"unknown tool: {name}")`` at the end — there is
    no handler registered for it.
    """
    name = normalize_tool_name(name)

    # Phase B (Issue #5/#6/#7/#15): resolve AuthContext from caller.
    ctx = _ctx_from_caller(caller)

    # Late import so patch.object(server_mod, "tool_name") is honoured.
    # server.py re-exports all tool functions; patches on server_mod override them.
    import app.mcp.server as _srv_mod

    _tool_ns = vars(_srv_mod)

    if name == "loopskill_search":
        return _tool_ns.get("loopskill_search", loopskill_search)(
            db,
            query=args.get("query"),
            category=args.get("category"),
            tier=args.get("tier"),
            limit=int(args.get("limit", 20)),
        )
    # ── loopskill_0622 Phase 8 / activate_0701 Phase A1: runnable catalog types ──
    # Canonical verifier names dispatch after normalize_tool_name maps legacy names.
    if name in ("loopskill_search_verifiers", "loopskill_search_loops"):
        return loopskill_search_loops(
            db, query=args.get("query"), category=args.get("category"), limit=int(args.get("limit", 50))
        )
    if name in ("loopskill_get_verifier", "loopskill_get_loop"):
        return loopskill_get_loop(db, slug=args["slug"], ctx=ctx)
    if name == "loopskill_search_personalities":
        return loopskill_search_personalities(
            db, query=args.get("query"), category=args.get("category"), limit=int(args.get("limit", 50))
        )
    if name == "loopskill_get_personality":
        return loopskill_get_personality(db, slug=args["slug"], ctx=ctx)
    _cl_result = _dispatch_composite_loop(name, db, args, ctx)
    if _cl_result is not _NOT_HANDLED:  # type: ignore[comparison-overlap]
        return _cl_result
    # ── activate_0701 Phase B: connector publish ──────────────────────────
    if name == "loopskill_connector_publish":
        fn = _tool_ns.get("loopskill_connector_publish", loopskill_connector_publish)
        return fn(db, ctx=ctx, **{k: v for k, v in args.items() if k != "ctx"})
    if name == "loopskill_install":
        return _tool_ns.get("loopskill_install", loopskill_install)(
            db,
            slug=args["slug"],
            api_key_id=caller.get("api_key_id"),
            ctx=ctx,
        )
    _lib_result = _dispatch_library(name, db, args, ctx)
    if _lib_result is not _NOT_HANDLED:  # type: ignore[comparison-overlap]
        return _lib_result
    if name == "loopskill_bundle_install":
        # cookbook_share_2105 Phase F. Map CookbookInstallError to the
        # standard {error, status, code} envelope MCP callers parse.
        try:
            return loopskill_bundle_install(
                db=db,
                ctx=ctx,
                cookbook_id=args.get("cookbook_id"),
                slug=args.get("slug"),
            )
        except CookbookInstallError as exc:
            return {"error": exc.message, "code": exc.code, "status": exc.status}
    # ── spotify_0608 Ph D: streaming bundle-composition verbs ─────────────
    if name == "loopskill_install_from_bundle":
        try:
            return _tool_ns.get("loopskill_install_from_bundle", loopskill_install_from_bundle)(
                db,
                link=args["link"],
                ctx=ctx,
            )
        except CookbookInstallError as exc:
            return {"error": exc.message, "code": exc.code, "status": exc.status}
    if name == "loopskill_pick_best_from_bundle":
        try:
            return _tool_ns.get("loopskill_pick_best_from_bundle", loopskill_pick_best_from_bundle)(
                db,
                link=args["link"],
                need=args.get("need"),
                ctx=ctx,
            )
        except CookbookInstallError as exc:
            return {"error": exc.message, "code": exc.code, "status": exc.status}
    if name == "loopskill_compose_bundle_from_links":
        try:
            return _tool_ns.get("loopskill_compose_bundle_from_links", loopskill_compose_bundle_from_links)(
                db,
                links=args["links"],
                name=args.get("name"),
                ctx=ctx,
            )
        except CookbookInstallError as exc:
            return {"error": exc.message, "code": exc.code, "status": exc.status}
    if name == "loopskill_list_bundle":
        return _tool_ns.get("loopskill_list_bundle", loopskill_list_bundle)(
            db,
            ctx=ctx,
            cookbook_id=args.get("cookbook_id"),
        )
    if name == "loopskill_recall":
        return _tool_ns.get("loopskill_recall", loopskill_recall)(db, **args)
    if name == "loopskill_skillify":
        return _tool_ns.get("loopskill_skillify", loopskill_skillify)(db, ctx=ctx, **args)
    if name == "loopskill_subskill_resolve":
        return _tool_ns.get("loopskill_subskill_resolve", loopskill_subskill_resolve)(db, **args)
    if name == "loopskill_doctor":
        return _tool_ns.get("loopskill_doctor", loopskill_doctor)(db, install_dir=args["install_dir"])
    if name == "loopskill_seeker":
        return _tool_ns.get("loopskill_seeker", loopskill_seeker)(db, **args)
    if name == "loopskill_sync":
        return _tool_ns.get("loopskill_sync", loopskill_sync)(
            db,
            cookbook_id=args["cookbook_id"],
            dry_run=args.get("dry_run", False),
            ctx=ctx,
        )
    if name == "loopskill_feedback":
        return _tool_ns.get("loopskill_feedback", loopskill_feedback)(
            db,
            category=args["category"],
            message=args["message"],
            context=args.get("context"),
            agent_id=args.get("agent_id"),
            force=args.get("force", False),
            confirmation=args.get("confirmation"),
            api_key_id=caller.get("api_key_id"),
            ctx=ctx,
            provenance_id=args.get("provenance_id"),
        )
    if name == "loopskill_request_skill":
        return _tool_ns.get("loopskill_request_skill", loopskill_request_skill)(
            db,
            target_name=args["target_name"],
            why_useful=args["why_useful"],
            suggested_sources=args.get("suggested_sources"),
            agent_id=args.get("agent_id"),
            api_key_id=caller.get("api_key_id"),
        )
    if name == "loopskill_report_skill_error":
        return _tool_ns.get("loopskill_report_skill_error", loopskill_report_skill_error)(
            db,
            slug=args["slug"],
            signature=args["signature"],
            summary=args["summary"],
            details=args.get("details"),
            agent_id=args.get("agent_id"),
            api_key_id=caller.get("api_key_id"),
            provenance_id=args.get("provenance_id"),
        )
    if name == "loopskill_propose_skill_patch":
        return _tool_ns.get("loopskill_propose_skill_patch", loopskill_propose_skill_patch)(
            db,
            slug=args["slug"],
            base_version=args["base_version"],
            files=args["files"],
            rationale=args["rationale"],
            evidence_install_id=args.get("evidence_install_id"),
            agent_id_anon=args.get("agent_id_anon"),
            api_key_id=caller.get("api_key_id"),
        )
    # ── Phase D: share-token management tools ───────────────────────────────
    if name == "loopskill_share_create":
        return _tool_ns.get("loopskill_share_create", loopskill_share_create)(
            db,
            cookbook_id=args["cookbook_id"],
            name=args.get("name"),
            scope=args.get("scope", "install"),
            ctx=ctx,
        )
    if name == "loopskill_share_list":
        return _tool_ns.get("loopskill_share_list", loopskill_share_list)(
            db,
            cookbook_id=args["cookbook_id"],
            ctx=ctx,
        )
    if name == "loopskill_share_revoke":
        return _tool_ns.get("loopskill_share_revoke", loopskill_share_revoke)(
            db,
            cookbook_id=args["cookbook_id"],
            token_id=args["token_id"],
            ctx=ctx,
        )
    if name == "loopskill_share_rotate":
        return _tool_ns.get("loopskill_share_rotate", loopskill_share_rotate)(
            db,
            cookbook_id=args["cookbook_id"],
            token_id=args["token_id"],
            ctx=ctx,
        )
    # Phase E: fleet tools
    if name == "loopskill_fleet_create":
        return _tool_ns.get("loopskill_fleet_create", loopskill_fleet_create)(
            db,
            name=args["name"],
            ctx=ctx,
        )
    if name == "loopskill_fleet_subscribe":
        return _tool_ns.get("loopskill_fleet_subscribe", loopskill_fleet_subscribe)(
            db,
            fleet_id=args["fleet_id"],
            cookbook_id=args["cookbook_id"],
            channel=args.get("channel", "stable"),
            ctx=ctx,
        )
    if name == "loopskill_fleet_sync":
        return _tool_ns.get("loopskill_fleet_sync", loopskill_fleet_sync)(
            db,
            fleet_id=args["fleet_id"],
            dry_run=args.get("dry_run", False),
            ctx=ctx,
        )
    if name == "loopskill_fleet_list":
        return _tool_ns.get("loopskill_fleet_list", loopskill_fleet_list)(
            db,
            ctx=ctx,
        )
    if name == "loopskill_publish_request":
        return _tool_ns.get("loopskill_publish_request", loopskill_publish_request)(
            db,
            slug=args["slug"],
            content=args["content"],
            version=args.get("version", "1.0.0"),
            description=args.get("description"),
            tier=args.get("tier", "pro"),
            is_public=args.get("is_public", True),
            references=args.get("references"),
            scripts=args.get("scripts"),
            license=args.get("license", "MIT"),
            changelog=args.get("changelog"),
            force=args.get("force", False),
            confirmation=args.get("confirmation"),
            api_key_id=caller.get("api_key_id"),
            ctx=ctx,
        )
    # integrator_2905 W1: tailor / fork tools
    if name == "loopskill_fork_list":
        return _tool_ns.get("loopskill_fork_list", loopskill_fork_list)(
            db,
            ctx=ctx,
        )
    if name == "loopskill_tailor":
        return _tool_ns.get("loopskill_tailor", loopskill_tailor)(
            db,
            source_slug=args["source_slug"],
            name=args["name"],
            readme=args.get("readme"),
            ctx=ctx,
        )
    if name == "loopskill_tailor_version":
        return _tool_ns.get("loopskill_tailor_version", loopskill_tailor_version)(
            db,
            fork_id=args["fork_id"],
            tarball_base64=args["tarball_base64"],
            semver=args["semver"],
            changelog=args.get("changelog"),
            ctx=ctx,
        )
    if name == "loopskill_bundle_attach":
        return _tool_ns.get("loopskill_bundle_attach", loopskill_bundle_attach)(
            db,
            fork_id=args["fork_id"],
            target_cookbook_id=args["target_cookbook_id"],
            slug=args.get("slug"),
            ctx=ctx,
        )
    if name == "loopskill_bundle_handoff":
        return _tool_ns.get("loopskill_bundle_handoff", loopskill_bundle_handoff)(
            db,
            ctx=ctx,
            cookbook_id=args["cookbook_id"],
            new_owner_user_id=args.get("new_owner_user_id"),
            new_owner_email=args.get("new_owner_email"),
            mode=args.get("mode", "transfer"),
        )
    # ── Phase J: user-routable feedback (THE MOAT) ──────────────────────────
    if name == "loopskill_configure_feedback":
        return _tool_ns.get("loopskill_configure_feedback", loopskill_configure_feedback)(
            db,
            repo=args.get("repo"),
            mode=args.get("mode"),
            pat=args.get("pat"),
            cookbook_id=args.get("cookbook_id"),
            ctx=ctx,
        )
    # Delegated dispatch chain (fleet write / placements / harvest) — see
    # app/mcp/dispatch_chain.py. Kept out of this god node for the 600-line gate.
    _r = run_dispatch_chain(name, db, args, ctx)
    if _r is not DISPATCH_NOT_HANDLED:
        return _r
    raise ValueError(f"unknown tool: {name}")


def call_tool_sync(
    name: str,
    args: dict[str, Any],
    *,
    caller: dict[str, Any] | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    """Direct synchronous entry-point used by tests and the stdio loop.

    Injects a ``bundle_status`` block when the caller is an authenticated
    user with outdated skills in their cookbooks.
    """
    caller = caller or {"scope": "operator", "user_id": None}  # legacy alias (pre-Phase-5 stdio default)
    own_db = db is None
    session = db or SessionLocal()
    # Normalise the name here too so the post-dispatch checks below match correctly.
    normalized_name = normalize_tool_name(name)
    try:
        payload = _dispatch(name, session, args or {}, caller)
        # Cache-invalidate + inject the status block. Lives in bundle_status so
        # the decoration sits next to the tenant clause that gates it (and so
        # this module stays under the 600-line god-object cap).
        return decorate_with_bundle_status(session, payload, caller, normalized_name)
    finally:
        if own_db:
            session.close()


from app.mcp.auth_propagate import _caller_from_request_context  # noqa: F401
from app.mcp.streaming import (  # noqa: F401
    _sse_transport,
    _http_session_manager,
    get_http_session_manager,
    _reset_http_session_manager,
    _build_streamable_http_mount,
    run_streamable_http,
    run_stdio,
)

logger = logging.getLogger("wiserecipes.mcp")

SERVER_NAME = "recipes-mcp"
SERVER_VERSION = "0.1.0"


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
        # Rationale: MCP tool dispatch errors must return error dict, not crash the transport
        except Exception as exc:  # noqa: BLE001
            payload = {"error": str(exc), "tool": name}
        finally:
            db.close()
        return [types.TextContent(type="text", text=json.dumps(payload, default=str))]

    return server


# ── FastAPI router (SSE transport) — public surface /api/mcp/{healthz,sse,messages/}
# Why /api/mcp not plain /mcp: the Cloudflare zone fronting app.loopskill.io
# 404s literal /mcp/* at the edge (CF AI Gateway); /api/mcp/* passes via the
# /api/* tunnel rule (verified 2026-05-07 via cloudflared_tunnel_total_requests).

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


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
    # Reject unrecognised AND missing key (defense-in-depth; caught by test_sse_rejects_missing_api_key).
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
    # Rationale: malformed MCP messages must return 400, not crash the SSE transport
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp message dispatch failed: %s", exc)
        return JSONResponse({"detail": "bad message"}, status_code=400)
    return Response(status_code=202)
