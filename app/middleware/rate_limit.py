"""RateLimitMiddleware — Redis-backed sliding-window rate limit per client IP.

get_redis and mark_redis_failed live in app.middleware.__init__ (not here)
so that patch("app.middleware.get_redis") is visible to _check_redis.
_check_redis uses a late import of app.middleware to honour the patch.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

import redis
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.utils.client_ip import _real_client_ip as _real_client_ip_from_utils  # Issue #12

logger = logging.getLogger("wiserecipes.middleware")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Redis-backed sliding-window rate limit per client IP.

    Per the spec: 60 req/min (configurable via RATE_LIMIT_PER_MINUTE).
    Uses Redis Sorted Sets for precise sliding window counting.
    Falls back to in-memory if Redis is down.

    Real client IP is taken from CF-Connecting-IP (Cloudflare) or the first
    entry in X-Forwarded-For when present, NOT request.client.host. Behind
    Cloudflare, request.client.host is the edge IP and shared across every
    visitor — which would put the entire internet into a single 60/min bucket.
    """

    # Paths we never rate-limit:
    # - docs / health: utility
    # - / : landing page
    # - /api/auth/*/login + /api/auth/*/callback : OAuth one-shot redirects.
    #   Limiting these by shared IP locks every visitor out of GitHub/Google
    #   sign-in once Cloudflare's pop has 60 hits in the window. The OAuth
    #   provider already enforces per-app rate limits server-side; double-
    #   limiting here breaks login without adding security.
    EXEMPT_PATHS = {
        "/docs",
        "/openapi.json",
        "/redoc",
        "/healthz",
        "/",
        "/api/healthz",
        "/api/health/transparency",
    }
    EXEMPT_PREFIXES = ("/api/auth/github/", "/api/auth/google/")

    def __init__(self, app, max_requests: int = 60, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # In-memory fallback
        self._hits: dict[str, list[float]] = defaultdict(list)

    @staticmethod
    def _real_client_ip(request: Request) -> str:
        """Get the actual visitor IP, respecting trusted-proxy CIDRs (Issue #12).

        Delegates to app.utils.client_ip._real_client_ip with the configured
        TRUSTED_PROXY_CIDRS so CF/XFF headers are only honoured when the
        direct TCP peer is a known Cloudflare edge IP.
        """
        return _real_client_ip_from_utils(request, settings.TRUSTED_PROXY_CIDRS)

    def _check_redis(self, client_ip: str, now: float) -> bool:
        """Check rate limit via Redis sliding window. Returns True if allowed."""
        # Late import so patch("app.middleware.get_redis") is honoured at call time.
        import app.middleware as _mw

        client = _mw.get_redis()
        if client is None:
            return None  # Signal to use in-memory fallback

        key = f"rate:{client_ip}"
        window_start = now - self.window_seconds
        pipe = client.pipeline()
        try:
            # Remove old entries outside the window
            pipe.zremrangebyscore(key, 0, window_start)
            # Count entries in current window
            pipe.zcard(key)
            # Add current request
            pipe.zadd(key, {str(now): now})
            # Set expiry on the key (cleanup)
            pipe.expire(key, self.window_seconds + 1)
            results = pipe.execute()

            count = results[1]  # zcard result (before adding current request)
            return count < self.max_requests
        except (redis.ConnectionError, redis.TimeoutError) as e:
            logger.warning("Redis rate limit check failed, using in-memory: %s", e)
            import app.middleware as _mw2

            _mw2.mark_redis_failed()
            return None

    def _check_memory(self, client_ip: str, now: float) -> bool:
        """In-memory fallback rate limit check."""
        window = self._hits[client_ip]
        self._hits[client_ip] = [t for t in window if now - t < self.window_seconds]
        self._hits[client_ip].append(now)
        return len(self._hits[client_ip]) <= self.max_requests

    async def dispatch(self, request: Request, call_next):
        if request.url.path in self.EXEMPT_PATHS:
            return await call_next(request)
        if any(request.url.path.startswith(p) for p in self.EXEMPT_PREFIXES):
            return await call_next(request)

        # Authenticated callers bypass the per-IP minute bucket.
        #
        # Why: APIKeyMiddleware runs BEFORE this middleware (Starlette LIFO
        # order — the last add_middleware in main.py is outermost on the
        # request path), so request.state.auth_ctx is already populated.
        # Real consumers — master scope, rec_live_* user keys, MCP fleet keys,
        # cookbook-bound CBT tokens, and the Astro portal build itself —
        # routinely fire dozens of requests in a few hundred ms from a single
        # IP. That instantly busts the 60/min bucket and the portal hero
        # falls back to a hardcoded count, the spotlight grid empties, etc.
        #
        # Anonymous traffic (no x-api-key, no master, no cookie) is still
        # bucketed — that's where IP-based limiting actually defends.
        #
        # Per-key abuse (a leaked rec_live_* key hammering the API from one
        # IP) is bounded by the install-route's per-key TIER_INSTALL_LIMITS,
        # not this middleware.
        auth_ctx = getattr(request.state, "auth_ctx", None)
        scope = getattr(auth_ctx, "scope", None) if auth_ctx else None
        if scope and scope != "anonymous":
            return await call_next(request)

        client_ip = self._real_client_ip(request)
        now = time.time()

        allowed = self._check_redis(client_ip, now)
        if allowed is None:
            allowed = self._check_memory(client_ip, now)

        if not allowed:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Try again later."},
            )

        return await call_next(request)
