"""WiseRecipes API — FastAPI application factory."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import settings
from app.admin_routes import router as admin_router
from app.auth_routes import router as auth_router
from app.api_key_routes import router as api_key_router
from app.buckets_routes import router as buckets_router
from app.carousel.routes import router as carousel_router
from app.checkout_routes import router as checkout_router
from app.creator_routes import router as creator_router
from app.database import engine
from app.buckets_routes import router as buckets_router
from app.discord_bot import bot as discord_bot
from app.feedback_routes import router as feedback_router
from app.canary import router as canary_router
from app.cookbook_routes import router as cookbook_router
from app.forks_routes import router as forks_router
from app.graph_routes import router as graph_router
from app.heartbeat_routes import router as heartbeat_router
from app.intent_survey_routes import router as intent_survey_router
from app.mcp.server import (
    router as mcp_router,
    run_streamable_http,
    get_http_session_manager,
)
from app.middleware import APIKeyMiddleware, BucketHostMiddleware, RateLimitMiddleware
from app.models import Base
from app.publisher_routes import router as publisher_router
from app.recall_routes import router as recall_router
from app.recipify_routes import router as recipify_router
from app.referral_routes import router as referral_router
from app.routes import router
from app.sandbox.routes import router as sandbox_router
from app.skill_error_routes import router as skill_error_router
from app.transparency_routes import router as transparency_router
from app.feedback_v1_routes import router as feedback_v1_router
from app.skill_patch_routes import router as skill_patch_router
from app.sse_routes import router as sse_router
from app.share_token_routes import router as share_token_router
from app.sync_fanout import get_fanout

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start/stop the Discord bot alongside the API.

    Bot is a no-op when DISCORD_BOT_TOKEN is empty (server doesn't exist
    yet at deploy time) — see app/discord_bot/bot.py.
    """
    bot_task = await discord_bot.start_bot()
    fanout = get_fanout()
    try:
        await fanout.start_listener()
    except Exception:
        logger.exception("fanout: failed to start LISTEN/NOTIFY worker (non-fatal)")
    # Phase 1 (v7.1): start StreamableHTTP session manager task group.
    streamable_http_cm = run_streamable_http()
    await streamable_http_cm.__aenter__()
    try:
        yield
    finally:
        try:
            await streamable_http_cm.__aexit__(None, None, None)
        except Exception:
            logger.exception("streamable_http: failed to shut down cleanly")
        try:
            await fanout.stop_listener()
        except Exception:
            logger.exception("fanout: failed to stop LISTEN/NOTIFY worker")
        await discord_bot.stop_bot(bot_task)


def create_app() -> FastAPI:
    app = FastAPI(
        title="WiseRecipes API",
        version="0.4.0",
        description="Skill marketplace & recipe sharing API for WiseChef ecosystem.",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Create tables
    Base.metadata.create_all(bind=engine)

    # Middleware (order: outermost first)
    app.add_middleware(RateLimitMiddleware, max_requests=settings.RATE_LIMIT_PER_MINUTE)
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(BucketHostMiddleware)

    app.include_router(router)
    app.include_router(admin_router)
    app.include_router(auth_router)
    app.include_router(carousel_router, prefix="/api")
    app.include_router(sandbox_router)
    app.include_router(creator_router)
    app.include_router(publisher_router)
    app.include_router(checkout_router)
    app.include_router(api_key_router)
    app.include_router(feedback_router)
    app.include_router(canary_router)
    app.include_router(forks_router)
    app.include_router(cookbook_router)
    app.include_router(graph_router)
    app.include_router(buckets_router)
    app.include_router(heartbeat_router)
    app.include_router(intent_survey_router)
    app.include_router(skill_error_router)
    app.include_router(transparency_router)
    app.include_router(feedback_v1_router)
    app.include_router(skill_patch_router)
    app.include_router(recall_router)
    app.include_router(recipify_router)
    app.include_router(referral_router)
    app.include_router(sse_router)
    app.include_router(share_token_router)
    app.include_router(mcp_router)

    # Phase 1 (v7.1): Mount StreamableHTTP ASGI sub-app at /api/mcp/http.
    # Must happen after include_router(mcp_router) so the session manager's
    # MCP server is built from the same build_mcp_server() factory.
    from app.mcp.server import _build_streamable_http_mount
    app.router.routes.append(_build_streamable_http_mount())

    @app.get("/", tags=["meta"])
    def root():
        return {"name": "WiseRecipes API", "version": "0.4.0", "docs": "/docs"}

    return app


app = create_app()
