"""WiseRecipes API — FastAPI application factory."""

from fastapi import FastAPI

from app.config import settings
from app.auth_routes import router as auth_router
from app.api_key_routes import router as api_key_router
from app.buckets_routes import router as buckets_router
from app.carousel.routes import router as carousel_router
from app.checkout_routes import router as checkout_router
from app.creator_routes import router as creator_router
from app.database import engine
from app.buckets_routes import router as buckets_router
from app.feedback_routes import router as feedback_router
from app.canary import router as canary_router
from app.forks_routes import router as forks_router
from app.graph_routes import router as graph_router
from app.middleware import APIKeyMiddleware, BucketHostMiddleware, RateLimitMiddleware
from app.models import Base
from app.publisher_routes import router as publisher_router
from app.routes import router
from app.sandbox.routes import router as sandbox_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="WiseRecipes API",
        version="0.4.0",
        description="Skill marketplace & recipe sharing API for WiseChef ecosystem.",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Create tables
    Base.metadata.create_all(bind=engine)

    # Middleware (order: outermost first)
    app.add_middleware(RateLimitMiddleware, max_requests=settings.RATE_LIMIT_PER_MINUTE)
    app.add_middleware(APIKeyMiddleware)
    app.add_middleware(BucketHostMiddleware)

    app.include_router(router)
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
    app.include_router(graph_router)
    app.include_router(buckets_router)

    @app.get("/", tags=["meta"])
    def root():
        return {"name": "WiseRecipes API", "version": "0.4.0", "docs": "/docs"}

    return app


app = create_app()
