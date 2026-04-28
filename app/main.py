"""WiseRecipes API — FastAPI application factory."""

from fastapi import FastAPI

from app.config import settings
from app.creator_routes import router as creator_router
from app.database import engine
from app.middleware import APIKeyMiddleware, RateLimitMiddleware
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

    app.include_router(router)
    app.include_router(sandbox_router)
    app.include_router(creator_router)
    app.include_router(publisher_router)

    @app.get("/", tags=["meta"])
    def root():
        return {"name": "WiseRecipes API", "version": "0.4.0", "docs": "/docs"}

    return app


app = create_app()
