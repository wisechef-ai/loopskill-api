"""APIKeyMiddleware + key validation helpers.

Handles x-api-key header validation: master key, rec_ user keys,
cbt_ share tokens (cookbook routes), and rec_fleet_ fleet keys.

NOTE: get_redis and mark_redis_failed live in app.middleware.__init__
so test patches via patch("app.middleware.get_redis") work correctly.
"""

import hashlib
import hmac
import logging
import time
from datetime import UTC

import redis
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

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


def _try_jwt_cookie_auth(request) -> bool:
    """Authenticate an authed route via the wr_jwt cookie. Returns success.

    Portal/OAuth users carry a ``wr_jwt`` cookie, not an ``x-api-key`` header.
    On a valid user-scope cookie, stamp ``auth_ctx`` + ``api_key_user_id`` so
    cookie auth and key auth converge (cookbook routes read api_key_user_id).
    The id is the real user UUID (never ``None``), so admin routes still reject.
    """
    jwt_ctx = _auth_ctx_from_jwt_cookie(request)
    if jwt_ctx is not None and getattr(jwt_ctx, "scope", None) == "user":
        request.state.auth_ctx = jwt_ctx
        request.state.api_key_user_id = jwt_ctx.user_id
        request.state.api_key_id = None
        return True
    return False


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
        # loopclose_3005 Phase B — canonical /skill serve (the install front-door
        # printed on the hero + every integrations card). MUST be public so an
        # agent can curl the meta-skill before it has a key. Serves the clean
        # in-repo SKILL.md as text/plain; no PII. (app/skill_serve_routes.py)
        "/skill",
        "/skill/",
        "/SKILL.md",
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
        "/api/cookbook-deploy/",  # spotify_0608 Ph A: re-homed from /api/buckets/
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
        "/api/bootcamp",  # bootcamp_0607 — curated install curricula, public discovery surface (list + detail)
        "/api/skills/search",
        "/api/skills/trending",
        "/api/skills/access",
        "/api/skills/_download",
        "/api/skills/external",  # evergreen_0206 F2 — external-only funnel: public discovery + fetch-origin install
        "/api/stats",
        "/api/forks/_download",
        "/api/graph",  # B.5: graph extension — public read; master-only write enforced inline
        # Phase D — anonymous heartbeat write endpoint (no API key required;
        # mathematically anonymous schema, see app/heartbeat_routes.py). The
        # READ endpoint /api/v1/fleet/weekly is gated separately (NOT public).
        "/api/v1/heartbeat",
        # Phase A v2 — MCP healthz/discovery is unauthenticated so MCP clients
        # can probe server availability before sending credentials. Actual SSE
        # transport (/api/mcp/sse) and message endpoint (/api/mcp/messages/)
        # remain auth-required and re-validate the key per request.
        "/api/mcp/healthz",
        # top1pct_1105 Phase A — marketing counts is the SSOT for every public
        # surface (hero, /skills, /pricing, /docs); reachable without auth so the
        # static-build pipeline can pull it; no PII.
        "/api/marketing/",
        # spotify_0608 Ph B — public cookbook discovery (CRUD stays auth-gated).
        "/api/cookbooks/discover",
        "/api/cookbooks/public/",
        # spotify_0608 Ph G — public reputation leaderboards (verify stays auth-gated).
        "/api/cookbooks/leaderboard",
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
            # CRITICAL: no DB lookup in the middleware (shared connection pool
            # gets confused by a parallel SessionLocal() mid-request). Instead:
            # if there's NO x-api-key header, mark "candidate free install" and
            # let the /install route enforce tier='free' + is_public at route
            # level (Depends(get_db) — same session, no double-session footgun).
            # The route's visibility check + is_anonymous_free_install together:
            #   - tier=free + public → install proceeds (no key)
            #   - tier=pro/pro_plus + no key → route returns 401
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
                # W0.1 (integrator_2905): /files (manifest) + /file (content)
                # are PUBLIC skill-detail sub-resources (Phase-Q file browser,
                # LarryBrain catalog UX) but were missing from the allow-list, so
                # middleware bare-401'd before the route ran (same class as the
                # 2026-05-19 P1 on /api/skills/access). /file keeps its own tier
                # paywall via request.state.auth_ctx; we stamp opportunistic auth
                # here (present key upgrades tier, absent key serves public).
                if (
                    slug
                    and not slug.startswith("_")
                    and slug not in self.PUBLIC_SKILL_DETAIL_AUTH_VERBS
                    and suffix in {"files", "file"}
                ):
                    api_key_ctx = _auth_ctx_from_api_key(request)
                    if api_key_ctx is not None:
                        request.state.auth_ctx = api_key_ctx
                    else:
                        request.state.auth_ctx = _auth_ctx_from_jwt_cookie(request)
                    return await call_next(request)
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
            # Portal/OAuth sessions authenticate by wr_jwt cookie, not x-api-key.
            # Honour a valid cookie before rejecting (see _try_jwt_cookie_auth).
            if _try_jwt_cookie_auth(request):
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing x-api-key header"},
            )

        # Enforce rec_ prefix — but first check for cbt_ share tokens
        if key.startswith("cbt_"):
            # SECURITY: cbt_ tokens are scoped strictly to cookbook routes — without
            # this gate they'd inherit the master-key signal (api_key_user_id=None)
            # on any endpoint using `is_master = (api_key_user_id is None)`. Anything
            # off the cookbook prefix → 403, no info leak. EXCEPTION (repohygiene_2605/
            # H.1, Issue #290): pro/pro_plus cbt_tokens with allow_public_catalog=True
            # may also call GET /api/skills/install + /_download for public-catalog
            # skills. Token is validated first (full DB lookup) so the path-broadening
            # only applies to genuine active tokens, not any cbt_-prefixed string.
            is_install_path = request.url.path in ("/api/skills/install", "/api/skills/_download")
            if not request.url.path.startswith("/api/cookbooks/") and not is_install_path:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Share tokens can only access cookbook routes"},
                )
            # Parse: cbt_<8-hex-prefix>_<32-hex-random>
            parts = key.split("_")
            if len(parts) != 3 or len(parts[1]) != 8 or len(parts[2]) != 32:
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
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Invalid or revoked share token"},
                    )
                # Found valid share token
                match.last_used_at = datetime.now(UTC)
                db.commit()

                # repohygiene_2605/H.1 (Issue #290): for install-path requests,
                # only tokens with allow_public_catalog=True are permitted.
                # Tokens without it (non-pro owners) still get 403 here so the
                # path-broadening doesn't accidentally grant access to free-tier owners.
                allow_pub = bool(getattr(match, "allow_public_catalog", False))
                if is_install_path and not allow_pub:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Share tokens can only access cookbook routes"},
                    )

                request.state.cookbook_token_scope = match.scope
                request.state.cookbook_token_cookbook_id = match.cookbook_id
                # SECURITY: do NOT set api_key_user_id=None — that's the master-key
                # sentinel. Use a string sentinel so any code that checks
                # `is_master = (api_key_user_id is None)` correctly excludes cbt_.
                request.state.api_key_user_id = "CBT_TOKEN"
                request.state.api_key_id = None
                request.state.is_cbt_token = True
                # auth_ctx: cbt_token scope — stamp allow_public_catalog for
                # downstream authz predicates + install_routes.
                from app.auth_ctx import AuthContext

                request.state.auth_ctx = AuthContext(
                    scope="cbt_token",
                    cookbook_scope=match.cookbook_id,
                    allow_public_catalog=allow_pub,
                )
                return await call_next(request)
            finally:
                db.close()

        if not key.startswith(API_KEY_PREFIX):
            return JSONResponse(
                status_code=401,
                content={"detail": f"API key must start with '{API_KEY_PREFIX}'"},
            )

        # Phase E: rec_fleet_* — fleet-scoped API keys. Ordered AFTER cbt_* and
        # BEFORE the master/rec_ paths so the distinct prefix is resolved first.
        # Format: rec_fleet_<8hex>_<32hex>. Stored as sha256 in Fleet.fleet_api_key_hash.
        if key.startswith(FLEET_KEY_PREFIX):
            from app.middleware._token_auth import resolve_fleet_auth_ctx

            fleet_ctx = resolve_fleet_auth_ctx(key)
            if fleet_ctx is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid or revoked fleet key"},
                )
            request.state.auth_ctx = fleet_ctx
            return await call_next(request)

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
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid API key"},
        )
