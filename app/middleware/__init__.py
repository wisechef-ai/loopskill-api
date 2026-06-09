"""app.middleware package — Phase J split + backward-compat shim.

This __init__.py re-exports every previously-public name from the original
app/middleware.py so that existing imports and test patches work unchanged:

    from app.middleware import APIKeyMiddleware     # still works
    patch("app.middleware.get_redis", ...)          # still works
    import app.middleware as mw; mw._redis_available = True  # still works

Redis module-level globals and the get_redis/mark_redis_failed helpers live
HERE (not in rate_limit.py) so that tests can mutate them directly.
"""

import logging
import time

import redis

from app.config import settings

logger = logging.getLogger("wiserecipes.middleware")


API_KEY_PREFIX = "rec_"
API_KEY_LENGTH = 36  # rec_ (4) + 32 hex chars
FLEET_KEY_PREFIX = "rec_fleet_"  # Phase E: fleet API keys (distinct from rec_live_, cbt_)

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


# Import submodules into this namespace (backward-compat re-exports)
# NOTE: submodules use late import of app.middleware inside function
# bodies to avoid circular imports while still honoring patches.
from app.middleware.api_key import (  # noqa: E402
    _auth_ctx_from_api_key,
    _auth_ctx_from_jwt_cookie,
    APIKeyMiddleware,
)
from app.middleware.cookbook_routing import CookbookHostMiddleware  # noqa: E402
from app.middleware.rate_limit import RateLimitMiddleware  # noqa: E402

__all__ = [
    "API_KEY_PREFIX",
    "API_KEY_LENGTH",
    "FLEET_KEY_PREFIX",
    "_redis_client",
    "_redis_available",
    "_redis_next_retry_at",
    "get_redis",
    "mark_redis_failed",
    "_auth_ctx_from_jwt_cookie",
    "_auth_ctx_from_api_key",
    "APIKeyMiddleware",
    "CookbookHostMiddleware",
    "RateLimitMiddleware",
]
