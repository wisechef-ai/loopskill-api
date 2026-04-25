"""Recipes API — FastAPI application factory."""

from fastapi import FastAPI

from app.config import settings
from app.database import engine
from app.middleware import APIKeyMiddleware, RateLimitMiddleware
from app.models import Base
from app.routes import router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Recipes API",
        version="0.2.0",
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

    @app.get("/", tags=["meta"])
    def root():
        return {"name": "Recipes API", "version": "0.2.0", "docs": "/docs"}

    return app


app = create_app()
