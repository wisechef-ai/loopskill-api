"""Auth middleware — x-api-key header validation + rate limiting.

Key format: rec_<32 hex chars> (Recipes brand prefix).
Rate limiting: in-memory sliding window (Redis upgrade tracked as future work).
"""

import hashlib
import time
from collections import defaultdict

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

API_KEY_PREFIX = "rec_"
API_KEY_LENGTH = 36  # rec_ (4) + 32 hex chars


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Validate x-api-key header. Exempt /docs, /openapi.json, /healthz, /."""

    EXEMPT_PATHS = {"/docs", "/openapi.json", "/redoc", "/healthz", "/", "/api/healthz"}

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        # Also exempt sub-paths under /docs
        if request.url.path.startswith("/docs/"):
            return await call_next(request)

        key = request.headers.get("x-api-key")
        if not key:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing x-api-key header"},
            )

        # Enforce rec_ prefix
        if not key.startswith(API_KEY_PREFIX):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=401,
                content={"detail": f"API key must start with '{API_KEY_PREFIX}'"},
            )

        # For dev: support a static master key from env (also prefixed rec_)
        if key == settings.API_KEY:
            return await call_next(request)

        # Production: hash the key and look up in api_keys table
        # TODO: implement DB-backed key lookup when we wire User/APIKey creation
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid API key"},
        )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory sliding-window rate limit per client IP.

    Per the spec: 60 req/min. Redis-backed rate limiting is the target
    but Redis is not yet deployed on this host; this in-memory version
    is functionally equivalent for single-instance deployment.
    """

    EXEMPT_PATHS = {"/docs", "/openapi.json", "/redoc", "/healthz", "/", "/api/healthz"}

    def __init__(self, app, max_requests: int = 60, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        window = self._hits[client_ip]
        # prune old entries
        self._hits[client_ip] = [t for t in window if now - t < self.window_seconds]
        self._hits[client_ip].append(now)

        if len(self._hits[client_ip]) > self.max_requests:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
            )

        return await call_next(request)
