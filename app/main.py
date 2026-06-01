"""WiseRecipes API — FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.access_routes import router as access_router  # Phase E: access split
from app.admin_routes import router as admin_router
from app.demand_routes import router as demand_router
from app.api_key_routes import router as api_key_router
from app.auth_routes import router as auth_router
from app.buckets_routes import router as buckets_router
from app.canary import router as canary_router
from app.carousel.routes import router as carousel_router
from app.checkout_routes import router as checkout_router
from app.config import settings
from app.cookbook_routes import router as cookbook_router
from app.creator_routes import router as creator_router
from app.credits_routes import router as credits_router
from app.discord_bot import bot as discord_bot
from app.feedback_routes import router as feedback_router
from app.feedback_status_routes import router as feedback_status_router
from app.feedback_v1_routes import router as feedback_v1_router
from app.forks_routes import router as forks_router
from app.graph_routes import router as graph_router
from app.health_routes import router as health_router  # Phase E: health split
from app.heartbeat_routes import router as heartbeat_router
from app.install_routes import router as install_router  # Phase E: install split
from app.intent_survey_routes import router as intent_survey_router
from app.internal_routes import router as internal_router
from app.marketing_routes import router as marketing_router
from app.marketing_routes import wisechef_router
from app.mcp.server import (
    router as mcp_router,
)
from app.mcp.server import (
    run_streamable_http,
)
from app.middleware import APIKeyMiddleware, BucketHostMiddleware, RateLimitMiddleware
from app.publisher_routes import router as publisher_router
from app.recall_routes import router as recall_router
from app.recipe_routes import router as recipe_router  # Phase E: recipe split
from app.recipify_routes import router as recipify_router
from app.referral_routes import router as referral_router
from app.routes import (
    router,
    utm_router,  # backwards-compat: routes.py re-exports from utm_redirects
)
from app.sandbox.routes import router as sandbox_router
from app.share_token_routes import router as share_token_router
from app.skill_error_routes import router as skill_error_router
from app.skill_patch_routes import router as skill_patch_router
from app.skill_routes import router as skill_router  # Phase E: skill split
from app.skill_files_routes import router as skill_files_router  # Phase Q: file surface
from app.skill_serve_routes import skill_serve_router  # loopclose_3005 B: canonical /skill
from app.sse_routes import router as sse_router
from app.startup_checks import check_alembic_heads, verify_stripe_webhook_endpoint  # Phase 4
from app.sync_fanout import get_fanout
from app.transparency_routes import router as transparency_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the Discord bot alongside the API.

    Bot is a no-op when DISCORD_BOT_TOKEN is empty (server doesn't exist
    yet at deploy time) — see app/discord_bot/bot.py.
    """
    # Phase 4: boot-time Stripe webhook smoke test (fail-soft).
    await verify_stripe_webhook_endpoint()
    bot_task = await discord_bot.start_bot()
    fanout = get_fanout()
    try:
        await fanout.start_listener()
    # Rationale: fanout LISTEN worker is non-critical; boot must succeed even if Redis/PG unavailable
    except Exception:  # noqa: BLE001
        logger.exception("fanout: failed to start LISTEN/NOTIFY worker (non-fatal)")
    # Phase 1 (v7.1): start StreamableHTTP session manager task group.
    streamable_http_cm = run_streamable_http()
    await streamable_http_cm.__aenter__()
    try:
        yield
    finally:
        try:
            await streamable_http_cm.__aexit__(None, None, None)
        # Rationale: MCP StreamableHTTP shutdown is best-effort; log but don't block
        except Exception:  # noqa: BLE001
            logger.exception("streamable_http: failed to shut down cleanly")
        try:
            await fanout.stop_listener()
        # Rationale: fanout stop is best-effort; log but don't block app shutdown
        except Exception:  # noqa: BLE001
            logger.exception("fanout: failed to stop LISTEN/NOTIFY worker")
        await discord_bot.stop_bot(bot_task)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app = FastAPI(
        title="WiseRecipes API",
        version="0.5.0",
        description="Skill marketplace & recipe sharing API for WiseChef ecosystem.",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Issue #21 (secfix_1905/H): replace the dev-only create_all shortcut with
    # an explicit alembic heads alignment check.  This refuses to start in
    # non-sqlite environments when migrations are behind, preventing the service
    # from silently running against a mismatched schema.
    check_alembic_heads()

    # Middleware (order: outermost first)
    # CORS: strict allow-list for production web origins.
    # MCP clients (AI agents, CLI tools) connect programmatically and do not send
    # browser Origin headers, so the restrictive list does not affect them.
    # The /api/mcp/http StreamableHTTP mount is also subject to this policy;
    # see docs/security/cors.md for the rationale and MCP considerations.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://recipes.wisechef.ai",
            "https://www.recipes.wisechef.ai",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["x-api-key", "authorization", "content-type"],
    )
    app.add_middleware(RateLimitMiddleware, max_requests=settings.RATE_LIMIT_PER_MINUTE)
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(BucketHostMiddleware)

    app.include_router(router)
    app.include_router(utm_router)  # marketing_1205: /x/<slug>, /li/<slug> etc.
    app.include_router(skill_serve_router)  # loopclose_3005 B: canonical GET /skill
    app.include_router(wisechef_router)  # Phase L: demo-funnel /api/wisechef/*
    app.include_router(health_router, prefix="/api", tags=["meta"])
    app.include_router(access_router, prefix="/api", tags=["skills"])
    app.include_router(recipe_router, prefix="/api", tags=["recipes"])
    app.include_router(install_router, prefix="/api", tags=["skills"])
    app.include_router(skill_router, prefix="/api", tags=["skills"])
    app.include_router(skill_files_router, prefix="/api", tags=["skills"])
    app.include_router(admin_router, tags=["admin"])
    app.include_router(demand_router, tags=["admin"])  # demandbrief_3005: content-direction feed
    app.include_router(auth_router, tags=["auth"])
    app.include_router(carousel_router, prefix="/api", tags=["carousel"])
    app.include_router(sandbox_router, tags=["sandbox"])
    app.include_router(creator_router, tags=["creator"])
    app.include_router(publisher_router, tags=["publisher"])
    app.include_router(checkout_router, tags=["billing"])
    app.include_router(api_key_router, tags=["api-keys"])
    app.include_router(feedback_router, tags=["feedback"])
    app.include_router(canary_router, tags=["canary"])
    app.include_router(forks_router, tags=["forks"])
    app.include_router(cookbook_router, tags=["cookbooks"])
    app.include_router(graph_router, tags=["graph"])
    app.include_router(buckets_router, tags=["buckets"])
    app.include_router(heartbeat_router, tags=["heartbeat"])
    app.include_router(intent_survey_router, tags=["surveys"])
    app.include_router(skill_error_router, tags=["skill-errors"])
    app.include_router(transparency_router, tags=["transparency"])
    app.include_router(feedback_v1_router, tags=["feedback"])
    app.include_router(skill_patch_router, tags=["skill-patches"])
    app.include_router(recall_router, tags=["recall"])
    app.include_router(recipify_router, tags=["recipify"])
    app.include_router(referral_router, tags=["referral"])
    app.include_router(marketing_router, tags=["marketing"])
    app.include_router(sse_router, tags=["sse"])
    app.include_router(share_token_router, tags=["share"])
    app.include_router(mcp_router, tags=["mcp"])
    app.include_router(internal_router)
    app.include_router(feedback_status_router)
    app.include_router(credits_router, tags=["credits"])

    # Phase 1 (v7.1): Mount StreamableHTTP ASGI sub-app at /api/mcp/http.
    # Must happen after include_router(mcp_router) so the session manager's
    # MCP server is built from the same build_mcp_server() factory.
    from app.mcp.server import _build_streamable_http_mount

    app.router.routes.append(_build_streamable_http_mount())

    @app.get("/", tags=["meta"])
    def root():
        return {"name": "WiseRecipes API", "version": "0.5.0", "docs": "/docs"}

    return app


app = create_app()
