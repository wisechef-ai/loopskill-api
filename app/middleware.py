"""Auth middleware — x-api-key header validation + Redis-backed rate limiting.

Key format: rec_<32 hex chars> (Recipes brand prefix).
Rate limiting: Redis sliding-window counter (survives restarts, shared across workers).
Falls back to in-memory if Redis is unavailable (graceful degradation).
"""

import hashlib
import time
import logging
from collections import defaultdict

import redis
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger("wiserecipes.middleware")

API_KEY_PREFIX="rec_"
API_KEY_LENGTH=36  # rec_ (4) + 32 hex chars

# Shared Redis client (lazy init)
_redis_client = None
_redis_available = None
_redis_next_retry_at: float = 0.0  # F-API-05: backoff timestamp


def get_redis():
    """Get Redis client with lazy initialization, health check, and 30s backoff."""
    global _redis_client, _redis_available, _redis_next_retry_at
    if _redis_client is not None and _redis_available:
        return _redis_client
    # F-API-05: if we're in the backoff window, skip retry
    if not _redis_available and time.monotonic() < _redis_next_retry_at:
        return None
    try:
        client = redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=2)
        client.ping()
        _redis_client = client
        _redis_available = True
        logger.info("Redis connected for rate limiting")
        return client
    except (redis.ConnectionError, redis.TimeoutError) as e:
        _redis_available = False
        _redis_next_retry_at = time.monotonic() + 30.0  # F-API-05: 30s backoff
        logger.warning("Redis unavailable, falling back to in-memory rate limiting: %s", e)
        return None


def mark_redis_failed():
    """Mark Redis as unavailable so next call retries connection."""
    global _redis_available
    _redis_available = None  # None = unknown, will retry on next request


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Validate x-api-key header. Exempt /docs, /openapi.json, /healthz, /,
    auth endpoints (JWT-based), Stripe webhooks, and public carousel endpoints."""

    EXEMPT_PATHS = {
        "/docs", "/openapi.json", "/redoc", "/healthz", "/", "/api/healthz",
    }
    # Prefixes for paths that use JWT auth instead of API key
    JWT_AUTH_PREFIXES = (
        "/api/auth/",
        "/api/stripe/onboard",
        "/api/stripe/status",
        "/api/stripe/dashboard",
        "/api/creator/",
        "/api/checkout/",
        "/api/billing/",
        "/api/api-keys",
        "/api/buckets/",
    )
    WEBHOOK_PATHS = {
        "/api/stripe/webhook",
    }
    # Public endpoints — no API key required (F4: carousel is unauthenticated;
    # G2: search/trending must be discoverable by agents before they have a key,
    # per LarryBrain spec §4.1; skill detail is public so agents can read SKILL.md
    # before deciding whether to subscribe — matches LarryBrain catalog browsing UX;
    # _download uses HMAC-signed token in the URL, no API key needed)
    PUBLIC_PREFIXES = (
        "/api/carousel/",
        "/api/skills/search",
        "/api/skills/trending",
        "/api/skills/access",
        "/api/skills/_download",
        "/api/stats",
        "/api/forks/_download",
        "/api/graph",  # B.5: graph extension — public read; master-only write enforced inline
    )
    # Public skill-detail GETs match path-shape /api/skills/{slug} (no trailing path).
    # Distinguished from /api/skills/install (auth) and /api/skills/_download (auth)
    # by checking the next segment doesn't start with underscore or known auth verb.
    PUBLIC_SKILL_DETAIL_AUTH_VERBS = ("install", "_download", "_publish", "_audit")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in self.EXEMPT_PATHS:
            return await call_next(request)

        if path.startswith("/docs/"):
            return await call_next(request)

        # Stripe webhook uses signature verification, not API key
        if path in self.WEBHOOK_PATHS:
            return await call_next(request)

        # JWT-authenticated endpoints don't need API key
        if any(path.startswith(prefix) for prefix in self.JWT_AUTH_PREFIXES):
            return await call_next(request)

        # Public endpoints — skip API key validation entirely (F4)
        if any(path.startswith(prefix) for prefix in self.PUBLIC_PREFIXES):
            return await call_next(request)

        # Public skill-detail GETs (/api/skills/{slug}) — match LarryBrain catalog
        # browsability. Auth-only verbs (install, _download, _publish) still gated.
        if request.method == "GET" and path.startswith("/api/skills/"):
            tail = path[len("/api/skills/"):]
            # Public full-graph dump (no slug, single segment)
            if tail == "graph":
                return await call_next(request)
            # Single segment, no underscore prefix, not a known auth verb.
            if "/" not in tail and not tail.startswith("_") and tail not in self.PUBLIC_SKILL_DETAIL_AUTH_VERBS:
                return await call_next(request)
            # Two-segment public sub-resources: /api/skills/{slug}/related (Stage 1, G15).
            # Only the suffix is allowed-listed — the slug itself is not parsed for
            # underscore/auth-verb rules because slugs are kebab-case lowercase.
            if "/" in tail:
                slug, _, suffix = tail.partition("/")
                if (
                    slug
                    and not slug.startswith("_")
                    and slug not in self.PUBLIC_SKILL_DETAIL_AUTH_VERBS
                    and suffix in {"related", "graph"}
                ):
                    return await call_next(request)

        # Admin endpoints require API key (not exempt)
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
            request.state.api_key_id = None
            request.state.api_key_user_id = None
            return await call_next(request)

        # Production: hash the key and look up in api_keys table
        from app.database import SessionLocal
        from app.models import APIKey
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        db = SessionLocal()
        try:
            api_key_obj = db.query(APIKey).filter(
                APIKey.key_hash == key_hash,
                APIKey.is_active == True,
            ).first()
            if api_key_obj:
                from datetime import datetime, timezone; api_key_obj.last_used_at = datetime.now(timezone.utc)
                db.commit()
                request.state.api_key_id = api_key_obj.id
                request.state.api_key_user_id = api_key_obj.user_id
                return await call_next(request)
        finally:
            db.close()

        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid API key"},
        )


class BucketHostMiddleware(BaseHTTPMiddleware):
    """White-label custom-domain routing for Studio buckets (Phase E.4).

    When a request arrives whose Host header matches a `buckets.custom_domain`
    row, the middleware stamps `request.state.bucket_id` and `bucket_slug`
    so downstream catalog handlers can scope responses to that bucket. The
    middleware is non-mutating for any non-bucket host and never alters the
    response body — scoping is opt-in by handlers reading `request.state`.
    """

    # Hosts that are NEVER treated as a custom domain regardless of DB state.
    SKIP_HOSTS = {"localhost", "127.0.0.1", "testserver"}

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower().strip()
        if not host or host in self.SKIP_HOSTS:
            return await call_next(request)

        from app.database import SessionLocal
        from app.models import Bucket

        db = SessionLocal()
        try:
            bucket = (
                db.query(Bucket)
                .filter(Bucket.custom_domain == host)
                .first()
            )
            if bucket:
                request.state.bucket_id = str(bucket.id)
                request.state.bucket_slug = bucket.slug
                request.state.bucket_theme = bucket.theme_json
        except Exception as e:  # noqa: BLE001
            logger.warning("BucketHostMiddleware lookup failed for host=%s: %s", host, e)
        finally:
            db.close()
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Redis-backed sliding-window rate limit per client IP.

    Per the spec: 60 req/min (configurable via RATE_LIMIT_PER_MINUTE).
    Uses Redis Sorted Sets for precise sliding window counting.
    Falls back to in-memory if Redis is down.
    """

    EXEMPT_PATHS = {"/docs", "/openapi.json", "/redoc", "/healthz", "/", "/api/healthz"}

    def __init__(self, app, max_requests: int = 60, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # In-memory fallback
        self._hits: dict[str, list[float]] = defaultdict(list)

    def _check_redis(self, client_ip: str, now: float) -> bool:
        """Check rate limit via Redis sliding window. Returns True if allowed."""
        client = get_redis()
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
            mark_redis_failed()
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

        client_ip = request.client.host if request.client else "unknown"
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
