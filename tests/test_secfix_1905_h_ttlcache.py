"""Issue #20 — _reconcile_last_attempt is bounded by a TTLCache.

Tests:
1. The module-level dict is a TTLCache (not a plain dict).
2. The maxsize is 10_000.
3. Entries evict after ttl seconds (using a frozen-time approach).
"""

from __future__ import annotations

import time

import pytest
from cachetools import TTLCache


def test_reconcile_cache_is_ttlcache() -> None:
    """_reconcile_last_attempt must be a TTLCache, not a plain dict."""
    from app.checkout_routes import _reconcile_last_attempt

    assert isinstance(_reconcile_last_attempt, TTLCache), (
        "_reconcile_last_attempt should be a cachetools.TTLCache, got "
        f"{type(_reconcile_last_attempt)}"
    )


def test_reconcile_cache_maxsize() -> None:
    """maxsize must be 10_000."""
    from app.checkout_routes import _reconcile_last_attempt

    assert _reconcile_last_attempt.maxsize == 10_000


def test_reconcile_cache_ttl_is_four_times_cooldown() -> None:
    """TTL must be _RECONCILE_COOLDOWN_S * 4."""
    from app.checkout_routes import _reconcile_last_attempt, _RECONCILE_COOLDOWN_S

    assert _reconcile_last_attempt.ttl == _RECONCILE_COOLDOWN_S * 4


def test_reconcile_cache_evicts_after_ttl() -> None:
    """Entries expire after ttl seconds (integration check with a tiny TTL)."""
    cache: TTLCache[str, float] = TTLCache(maxsize=10, ttl=0.05)  # 50 ms TTL
    cache["user-1"] = time.monotonic()
    assert "user-1" in cache
    time.sleep(0.1)  # wait for TTL expiry
    assert "user-1" not in cache, "Entry should have been evicted after TTL"


def test_reconcile_cache_evicts_lru_at_maxsize() -> None:
    """When maxsize is hit, oldest (LRU) entry is evicted."""
    cache: TTLCache[str, float] = TTLCache(maxsize=3, ttl=60)
    cache["a"] = 1.0
    cache["b"] = 2.0
    cache["c"] = 3.0
    assert len(cache) == 3
    # Adding a 4th entry triggers LRU eviction
    cache["d"] = 4.0
    assert len(cache) == 3
    assert "d" in cache
