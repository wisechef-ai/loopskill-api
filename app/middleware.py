"""Auth middleware — x-api-key header validation + Redis-backed rate limiting.

Key format: rec_<32 hex chars> (Recipes brand prefix).
Rate limiting: Redis sliding-window counter (survives restarts, shared across workers).
Falls back to in-memory if Redis is unavailable (graceful degradation).
"""

import hashlib
import hmac
import logging
import time
from collections import defaultdict
from datetime import UTC

import redis
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.utils.client_ip import _real_client_ip as _real_client_ip_from_utils  # Issue #12

logger = logging.getLogger("wiserecipes.middleware")

API_KEY_PREFIX = "rec_"
API_KEY_LENGTH = 36  # rec_ (4) + 32 hex chars
FLEET_KEY_PREFIX = "rec_fleet_"  # Phase E: fleet API keys (distinct from rec_live_, cbt_)

# Shared Redis client (lazy init)
_redis_client = None
_redis_available = None
_redis_next_retry_at: float = 0.0  # F-API-05: backoff timestamp


def _auth_ctx_from_jwt_cookie(request) -> "AuthContext":
    """Return an AuthContext populated from the wr_jwt cookie / Bearer token.

    Used on public skill-detail GETs where no x-api-key is present.  If the
    cookie is absent or invalid, returns AuthContext.anonymous() so downstream
    handlers always have a valid auth_ctx to inspect.

    Resolution order:
      1. ``wr_jwt`` cookie (browser portal sessions)
      2. ``Authorization: Bearer <token>`` (SPA clients, backward compat)

    Issue #25 (secfix_1905/H): extracted from the deleted _resolve_caller_tier
    helper so that JWT-cookie callers on public routes are properly hydrated
    into auth_ctx without the route needing a separate DB call.
    """
    from app.auth_ctx import AuthContext

    token = request.cookies.get("wr_jwt")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return AuthContext.anonymous()

    try:
        from app.auth_routes import verify_jwt  # local import to avoid cycles

        payload = verify_jwt(token)
    # Rationale: any JWT validation failure must not crash public skill-detail — return anonymous
    except Exception:  # noqa: BLE001
        return AuthContext.anonymous()

    if not payload:
        return AuthContext.anonymous()

    from uuid import UUID

    try:
        user_id = UUID(payload["sub"])
    except (ValueError, KeyError, TypeError):
        return AuthContext.anonymous()

    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.subscription_status in ("active", "trialing"):
            return AuthContext(
                scope="user",
                user_id=user_id,
                tier=user.subscription_tier,
            )
    finally:
        db.close()

    return AuthContext.anonymous()


def _auth_ctx_from_api_key(request) -> "AuthContext | None":
    """Opportunistically resolve an ``x-api-key`` header into an AuthContext.

    Used on PUBLIC routes (``/api/skills/access``) and on public skill-detail
    GETs, where the request is *allowed* without a key but, if a key IS
    present, the caller's tier / scope must still be honoured. Before this
    helper existed, both code paths short-circuited before the key was ever
    inspected, so an authenticated agent was indistinguishable from an
    anonymous one — ``/api/skills/access`` always reported ``user_tier=null``
    and the skill-detail body paywall never opened for x-api-key callers.

    Returns:
        * ``AuthContext(scope="master")`` for the master key.
        * ``AuthContext(scope="user", tier=…)`` for a valid ``rec_`` key.
        * ``None`` when no key is present, the key is malformed, or the key
          does not validate. ``None`` means "fall back to the JWT cookie /
          anonymous path" — it never turns a public route into a 401.

    This is read-only and never mutates request state; callers decide what to
    stamp onto ``request.state.auth_ctx``.
    """
    from app.auth_ctx import AuthContext

    key = request.headers.get("x-api-key")
    if not key or not key.startswith(API_KEY_PREFIX):
        # No key, or a cbt_ share token (handled only on cookbook routes) —
        # nothing to resolve here.
        return None

    # Master key — timing-safe comparison, mirrors the main validation path.
    if hmac.compare_digest(key, settings.API_KEY):
        return AuthContext(scope="master")

    from app.database import SessionLocal
    from app.models import APIKey, User

    db = SessionLocal()
    try:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        api_key_obj = (
            db.query(APIKey)
            .filter(APIKey.key_hash == key_hash, APIKey.is_active == True)  # noqa: E712
            .first()
        )
        if api_key_obj is None:
            return None
        user_obj = db.query(User).filter(User.id == api_key_obj.user_id).first()
        tier: str | None = None
        if user_obj and user_obj.subscription_status in ("active", "trialing"):
            tier = user_obj.subscription_tier
        return AuthContext(
            scope="user",
            user_id=api_key_obj.user_id,
            api_key_id=api_key_obj.id,
            cookbook_scope=api_key_obj.cookbook_id,
            is_sandbox_operator=bool(getattr(api_key_obj, "is_sandbox_operator", False)),
            tier=tier,
        )
    # Rationale: opportunistic auth on a public route must never crash the
    # request — any lookup failure degrades to anonymous (return None).
    except Exception:  # noqa: BLE001
        return None
    finally:
        db.close()


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
        "/docs",
        "/openapi.json",
        "/redoc",
        "/healthz",
        "/",
        "/api/healthz",
        "/api/health/transparency",  # Stream 0: public transparency scorecard
    }
    # Prefixes for paths that use JWT auth instead of API key
    JWT_AUTH_PREFIXES = (
        "/api/auth/",
        "/api/me/",  # WIS-660 referral routes (and any future /api/me/* user-scoped JWT endpoints)
        "/api/stripe/onboard",
        "/api/stripe/status",
        "/api/stripe/dashboard",
        "/api/creator/",
        "/api/checkout/",
        "/api/billing/",
        "/api/api-keys",
        "/api/buckets/",
        "/api/subscriptions/",  # subscriptions/downgrade is JWT-authed
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
        # Phase D — anonymous heartbeat write endpoint (no API key required;
        # mathematically anonymous schema, see app/heartbeat_routes.py).
        # The READ endpoint /api/v1/fleet/weekly is gated separately and is
        # NOT prefixed-public.
        "/api/v1/heartbeat",
        # Phase A v2 — MCP healthz/discovery is unauthenticated so MCP clients
        # can probe server availability before sending credentials. Actual SSE
        # transport (/api/mcp/sse) and message endpoint (/api/mcp/messages/)
        # remain auth-required and re-validate the key per request.
        "/api/mcp/healthz",
        # top1pct_1105 Phase A — marketing counts is the SSOT for every public
        # surface (homepage hero, /skills, /pricing, /docs). MUST be reachable
        # without auth so the static-build pipeline can pull it; counts contain
        # no PII or sensitive data.
        "/api/marketing/",
        # marketing_1205 — UTM redirectors for social platforms. Public, set cookie + 302.
        "/x/",
        "/li/",
        "/ig/",
        "/yt/",
        "/fb/",
    )

    # Phase A — POST /api/intent-survey is anonymous; GET /api/intent-survey/results
    # is admin-gated at the route level via x-api-key. Method-aware allowlist.
    PUBLIC_POST_ONLY_PATHS = {
        "/api/intent-survey",
    }
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

        # Public endpoints — skip API key validation entirely (F4).
        # Opportunistic auth: a key is NOT required here, but if one IS present
        # the caller's scope/tier must still be honoured. Without this, routes
        # like /api/skills/access read request.state.auth_ctx.tier and always
        # saw None even for a valid key, because this branch returned before
        # any auth_ctx was stamped. Bug A fix (repo-topclass P1).
        if any(path.startswith(prefix) for prefix in self.PUBLIC_PREFIXES):
            api_key_ctx = _auth_ctx_from_api_key(request)
            if api_key_ctx is not None:
                request.state.auth_ctx = api_key_ctx
            else:
                # No / invalid key: still stamp an auth_ctx so handlers always
                # have one. Honour a wr_jwt cookie if present, else anonymous.
                request.state.auth_ctx = _auth_ctx_from_jwt_cookie(request)
            return await call_next(request)

        # Method-aware public POST endpoints (intent-survey: anonymous submit only)
        if request.method == "POST" and path in self.PUBLIC_POST_ONLY_PATHS:
            return await call_next(request)

        # Public skill-detail GETs (/api/skills/{slug}) — match LarryBrain catalog
        # browsability. Auth-only verbs (install, _download, _publish) still gated,
        # EXCEPT /api/skills/install for tier=free skills (polish_1805 item 1 —
        # frictionless install matches Smithery `npx skills add` UX). Free-tier
        # installs are still rate-limited per IP by the RateLimitMiddleware so
        # this is not an abuse vector.
        if request.method == "GET" and path.startswith("/api/skills/"):
            tail = path[len("/api/skills/") :]
            # Public full-graph dump (no slug, single segment)
            if tail == "graph":
                return await call_next(request)
            # polish_1805 item 1 — public install for free skills only.
            # CRITICAL: we do NOT do a DB lookup in the middleware because the
            # test infrastructure (and some prod request-scoping) shares a
            # connection pool that gets confused by a parallel SessionLocal()
            # call mid-request. Instead: if the request has NO ``x-api-key``
            # header at all, mark it as "candidate free install" and let the
            # /install route enforce the tier='free' + is_public check at
            # the route level (route uses Depends(get_db) — same session as
            # the rest of the route logic, no double-session footgun).
            #
            # The route's existing visibility check + the new
            # ``is_anonymous_free_install`` gate together guarantee that:
            #   - tier=free + public → install proceeds (no key)
            #   - tier=cook/operator + no key → route returns 401
            #   - private skill + no key → route returns 404 (no leak)
            if tail == "install":
                has_key_header = bool(request.headers.get("x-api-key"))
                if not has_key_header:
                    request.state.api_key_user_id = None
                    request.state.api_key_id = None
                    request.state.is_anonymous_free_install = True
                    # auth_ctx: anonymous caller (Phase A wiring)
                    from app.auth_ctx import AuthContext

                    request.state.auth_ctx = AuthContext.anonymous()
                    return await call_next(request)
            # Single segment, no underscore prefix, not a known auth verb.
            if (
                "/" not in tail
                and not tail.startswith("_")
                and tail not in self.PUBLIC_SKILL_DETAIL_AUTH_VERBS
            ):
                # Issue #25 (secfix_1905/H): opportunistic auth for public
                # skill-detail GETs. Browsers carry a wr_jwt cookie; agents
                # carry an x-api-key header. BOTH must be honoured so the
                # Phase-B body paywall opens for any paid caller.
                # Bug B fix (repo-topclass P1): previously only the cookie was
                # read here, so a paid agent authenticating by x-api-key
                # resolved as anonymous and saw readme=null.
                api_key_ctx = _auth_ctx_from_api_key(request)
                if api_key_ctx is not None:
                    request.state.auth_ctx = api_key_ctx
                else:
                    request.state.auth_ctx = _auth_ctx_from_jwt_cookie(request)
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

        # Enforce rec_ prefix — but first check for cbt_ share tokens
        if key.startswith("cbt_"):
            # SECURITY: cbt_ tokens are scoped strictly to cookbook routes.
            # Without this gate they would inherit the master-key signal
            # (api_key_user_id=None) on any other endpoint that uses
            # `is_master = (api_key_user_id is None)`. Cookbook-prefixed paths
            # ONLY — anything else is 403 with no info leak.
            if not request.url.path.startswith("/api/cookbooks/"):
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=403,
                    content={"detail": "Share tokens can only access cookbook routes"},
                )
            # Parse: cbt_<8-hex-prefix>_<32-hex-random>
            parts = key.split("_")
            if len(parts) != 3 or len(parts[1]) != 8 or len(parts[2]) != 32:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid share token format"},
                )
            cookbook_prefix_8 = parts[1]
            from datetime import datetime

            from app.database import SessionLocal
            from app.models import CookbookShareToken

            db = SessionLocal()
            try:
                candidates = (
                    db.query(CookbookShareToken)
                    .filter(
                        CookbookShareToken.token_prefix == cookbook_prefix_8,
                        CookbookShareToken.is_active == True,
                    )
                    .all()
                )
                key_hash = hashlib.sha256(key.encode()).hexdigest()
                match = None
                for row in candidates:
                    if hmac.compare_digest(row.token_hash, key_hash):
                        match = row
                        break
                if match is None:
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Invalid or revoked share token"},
                    )
                # Found valid share token
                match.last_used_at = datetime.now(UTC)
                db.commit()
                request.state.cookbook_token_scope = match.scope
                request.state.cookbook_token_cookbook_id = match.cookbook_id
                # SECURITY: do NOT set api_key_user_id=None — that's the master-key
                # sentinel. Use a string sentinel so any code that checks
                # `is_master = (api_key_user_id is None)` correctly excludes cbt_.
                request.state.api_key_user_id = "CBT_TOKEN"
                request.state.api_key_id = None
                request.state.is_cbt_token = True
                # auth_ctx: cbt_token scope
                from app.auth_ctx import AuthContext

                request.state.auth_ctx = AuthContext(
                    scope="cbt_token",
                    cookbook_scope=match.cookbook_id,
                )
                return await call_next(request)
            finally:
                db.close()

        if not key.startswith(API_KEY_PREFIX):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=401,
                content={"detail": f"API key must start with '{API_KEY_PREFIX}'"},
            )

        # Phase E: rec_fleet_* — fleet-scoped API keys. Ordered AFTER cbt_* and
        # BEFORE the master/rec_ paths so the distinct prefix is resolved first.
        # Format: rec_fleet_<8hex>_<32hex>. Stored as sha256 in Fleet.fleet_api_key_hash.
        if key.startswith(FLEET_KEY_PREFIX):
            from app.database import SessionLocal
            from app.models import Fleet as _Fleet

            _fleet_key_hash = hashlib.sha256(key.encode()).hexdigest()
            _fleet_db = SessionLocal()
            try:
                _fleet_row = (
                    _fleet_db.query(_Fleet).filter(_Fleet.fleet_api_key_hash == _fleet_key_hash).first()
                )
                if _fleet_row is None:
                    from fastapi.responses import JSONResponse

                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Invalid or revoked fleet key"},
                    )
                from app.auth_ctx import AuthContext

                request.state.auth_ctx = AuthContext(
                    scope="fleet",
                    fleet_id=_fleet_row.id,
                    user_id=_fleet_row.owner_user_id,
                )
                return await call_next(request)
            finally:
                _fleet_db.close()

        # For dev: support a static master key from env (also prefixed rec_)
        if hmac.compare_digest(key, settings.API_KEY):
            request.state.api_key_id = None
            request.state.api_key_user_id = None
            # auth_ctx: master scope
            from app.auth_ctx import AuthContext

            request.state.auth_ctx = AuthContext(scope="master")
            return await call_next(request)

        # Production: hash the key and look up in api_keys table
        from app.database import SessionLocal
        from app.models import APIKey

        key_hash = hashlib.sha256(key.encode()).hexdigest()
        db = SessionLocal()
        try:
            api_key_obj = (
                db.query(APIKey)
                .filter(
                    APIKey.key_hash == key_hash,
                    APIKey.is_active == True,
                )
                .first()
            )
            if api_key_obj:
                # Issue #17: instead of committing to DB on every request,
                # push to Redis-batched tracker (drained by crons/drain_last_used.py).
                from datetime import datetime

                from app.last_used_tracker import tracker as _last_used_tracker

                _last_used_tracker.record(api_key_obj.id, datetime.now(UTC))
                request.state.api_key_id = api_key_obj.id
                request.state.api_key_user_id = api_key_obj.user_id
                # auth_ctx: user scope — Phase B stamps cookbook_scope from api_key.cookbook_id
                # Issue #25 (secfix_1905/H): stamp tier from User so routes can use
                # request.state.auth_ctx.tier instead of a separate DB lookup.
                from app.models import User as _User

                _user_obj = db.query(_User).filter(_User.id == api_key_obj.user_id).first()
                _tier: str | None = None
                if _user_obj and _user_obj.subscription_status in ("active", "trialing"):
                    _tier = _user_obj.subscription_tier

                from app.auth_ctx import AuthContext

                request.state.auth_ctx = AuthContext(
                    scope="user",
                    user_id=api_key_obj.user_id,
                    api_key_id=api_key_obj.id,
                    # secfix_1905/B: cookbook-scoped key restriction (Issue #13)
                    cookbook_scope=api_key_obj.cookbook_id,
                    # secfix_1905/C: propagate sandbox execution privilege
                    is_sandbox_operator=bool(getattr(api_key_obj, "is_sandbox_operator", False)),
                    # secfix_1905/H: subscription tier for paywall checks (#25)
                    tier=_tier,
                )
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
            bucket = db.query(Bucket).filter(Bucket.custom_domain == host).first()
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
