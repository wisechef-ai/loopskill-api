"""Per-agent reconcile abuse ceiling — evergreen_0206 Phase A.

Decision #20 (locked): free is NOT speed-throttled. Tiers separate on
CAPABILITY (free=single-cookbook+one-manual-sync · pro=auto-reconcile · pro+=fleet),
never on reconcile speed. With subscribe-by-default (SSE) + 304-fast-path +
Cloudflare, a normal agent's reconcile costs ~zero — there is nothing
legitimate to throttle.

This ceiling exists ONLY to stop deliberate abuse: a script that ignores the
subscribe model and spams the reconcile endpoint to exhaust the 30-connection
DB pool (premortem #3). It is:

  * keyed per ``api_key_id`` (NOT per IP — behind Cloudflare many agents share
    an edge IP; and a single key is the abuse unit anyway),
  * IDENTICAL for every tier (free, pro, pro_plus) — generous enough that no
    honest client ever trips it,
  * Redis-backed sliding window, reusing the exact sorted-set pattern proven in
    app/middleware/rate_limit.py (no second mechanism),
  * fail-OPEN on Redis outage (a memory blip must never lock honest agents out
    of keeping their skills evergreen — the DB pool has its own guards).

Wired into the reconcile endpoint in Phase D. Defined + tested here in Phase A
because the reconcile-contract (docs/reconcile-contract.md §4) declares it part
of the foundation.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import redis

logger = logging.getLogger("wiserecipes.abuse_ceiling")

# Decision #20: generous, same all tiers. 60 reconcile requests / 5 min per key.
RECONCILE_CEILING_REQUESTS = 60
RECONCILE_CEILING_WINDOW_SECONDS = 300

# In-memory fallback when Redis is unreachable (fail-open — see module docstring).
_mem_hits: dict[str, list[float]] = defaultdict(list)


@dataclass(frozen=True)
class CeilingResult:
    """Outcome of an abuse-ceiling check."""

    allowed: bool
    count: int  # requests seen in the window BEFORE this one
    retry_after: int  # seconds the caller should wait when blocked (0 when allowed)


def _check_redis(api_key_id: str, now: float) -> CeilingResult | None:
    """Sliding-window check via Redis sorted set. None → Redis unavailable."""
    # Late import so patch("app.middleware.get_redis") is honoured at call time,
    # matching the pattern in app/middleware/rate_limit.py.
    import app.middleware as _mw

    client = _mw.get_redis()
    if client is None:
        return None

    key = f"reconcile_abuse:{api_key_id}"
    window_start = now - RECONCILE_CEILING_WINDOW_SECONDS
    try:
        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        pipe.zadd(key, {f"{now}": now})
        pipe.expire(key, RECONCILE_CEILING_WINDOW_SECONDS + 1)
        results = pipe.execute()
        count = results[1]  # zcard BEFORE adding the current request
        allowed = count < RECONCILE_CEILING_REQUESTS
        retry_after = 0 if allowed else RECONCILE_CEILING_WINDOW_SECONDS
        return CeilingResult(allowed=allowed, count=count, retry_after=retry_after)
    except (redis.ConnectionError, redis.TimeoutError) as e:
        logger.warning("reconcile abuse-ceiling Redis check failed, failing open: %s", e)
        import app.middleware as _mw2

        _mw2.mark_redis_failed()
        return None


def _check_memory(api_key_id: str, now: float) -> CeilingResult:
    """In-memory fallback. Process-local; best-effort only."""
    window = _mem_hits[api_key_id]
    window = [t for t in window if now - t < RECONCILE_CEILING_WINDOW_SECONDS]
    count = len(window)
    window.append(now)
    _mem_hits[api_key_id] = window
    allowed = count < RECONCILE_CEILING_REQUESTS
    retry_after = 0 if allowed else RECONCILE_CEILING_WINDOW_SECONDS
    return CeilingResult(allowed=allowed, count=count, retry_after=retry_after)


def check_reconcile_abuse_ceiling(api_key_id: str, now: float | None = None) -> CeilingResult:
    """Return whether this api_key_id may make another reconcile request.

    Same ceiling for every tier (decision #20). Fail-open on Redis outage:
    keeping honest agents evergreen matters more than a perfect cap during a
    Redis blip — the DB pool has its own ceiling + PgBouncer trigger.

    Args:
        api_key_id: the caller's APIKey row id (the abuse unit).
        now: epoch seconds; injectable for tests.
    """
    if not api_key_id:
        # No key → can't account per-agent. Allow (anonymous reconcile is gated
        # elsewhere by ownership). Never key the ceiling on an empty string.
        return CeilingResult(allowed=True, count=0, retry_after=0)

    now = time.time() if now is None else now
    result = _check_redis(api_key_id, now)
    if result is None:
        result = _check_memory(api_key_id, now)
    return result


def _reset_for_tests() -> None:
    """Clear the in-memory fallback window (test hygiene)."""
    _mem_hits.clear()
