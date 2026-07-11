"""Tests for the §7 hot-query cache (metasearch_0710 P5)."""

from __future__ import annotations

import pytest
from concurrent.futures import ThreadPoolExecutor

import time

from app.services.metasearch_cache import CacheEntry, HotQueryCache, get_cache


def test_put_then_get_returns_cached_skills():
    c = HotQueryCache(ttl_s=60)
    c.put("browser", ("skills-sh", "clawhub"), [{"slug": "a"}], sources_ok=["skills-sh"])
    entry = c.get("browser", ("clawhub", "skills-sh"))  # order-independent
    assert entry is not None
    assert entry.skills == [{"slug": "a"}]
    assert entry.sources_ok == ["skills-sh"]


def test_miss_returns_none():
    c = HotQueryCache()
    assert c.get("nonexistent", ("skills-sh",)) is None


def test_expired_entry_evicted_on_get():
    c = HotQueryCache(ttl_s=0.01)
    c.put("old", ("skills-sh",), [{"slug": "a"}])
    time.sleep(0.02)
    assert c.get("old", ("skills-sh",)) is None


def test_query_normalization_collapses_variations():
    """§7: "Browser " and "browser" share a cache entry (popular queries collapse)."""
    c = HotQueryCache(ttl_s=60)
    c.put("  Browser  ", ("skills-sh",), [{"slug": "a"}])
    assert c.get("browser", ("skills-sh",)) is not None
    assert c.get("BROWSER", ("skills-sh",)) is not None


def test_lru_eviction_at_capacity():
    c = HotQueryCache(ttl_s=60, max_entries=3)
    for i in range(4):
        c.put(f"q{i}", ("skills-sh",), [{"slug": f"s{i}"}])
    # q0 was the oldest → evicted
    assert c.get("q0", ("skills-sh",)) is None
    assert c.get("q3", ("skills-sh",)) is not None


def test_hit_rate_stats():
    c = HotQueryCache(ttl_s=60)
    c.put("browser", ("skills-sh",), [{"slug": "a"}])
    c.get("browser", ("skills-sh",))  # hit
    c.get("browser", ("skills-sh",))  # hit
    c.get("missing", ("skills-sh",))  # miss
    stats = c.stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.667


def test_invalidate_clears_entries():
    c = HotQueryCache(ttl_s=60)
    c.put("browser", ("skills-sh",), [{"slug": "a"}])
    c.put("scraper", ("skills-sh",), [{"slug": "b"}])
    n = c.invalidate()
    assert n == 2
    assert c.get("browser", ("skills-sh",)) is None


def test_cache_entry_to_response_meta():
    e = CacheEntry(
        skills=[],
        sources_ok=["a"],
        sources_degraded=[],
        sources_failed=[],
        cached_at=time.monotonic(),
        ttl_s=300,
    )
    meta = e.to_response_meta()
    assert meta["cache_hit"] is True
    assert meta["cache_ttl_s"] == 300
    assert "cache_age_s" in meta


def test_get_cache_singleton():
    a = get_cache()
    b = get_cache()
    assert a is b


# ── single-flight concurrent regression (council P5 R3/R4) ────────────────────


def test_concurrent_cold_miss_calls_compute_exactly_once():
    """Council R3 MUST: N concurrent cold-miss callers → exactly ONE compute_fn
    call; the rest are waiters that read the cached result."""
    import threading

    c = HotQueryCache(ttl_s=60)
    call_count = {"n": 0}
    barrier = threading.Barrier(8)

    def _compute():
        call_count["n"] += 1
        time.sleep(0.05)  # simulate fan-out latency
        return [{"slug": "a"}], ["skills-sh"], []

    def _worker():
        barrier.wait()  # release all threads simultaneously
        return c.get_or_compute(("browser", ("skills-sh",)), _compute)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_worker) for _ in range(8)]
        results = [f.result() for f in futures]

    assert call_count["n"] == 1, f"exactly one compute, got {call_count['n']}"
    # all callers got a non-None entry
    for entry, _ in results:
        assert entry is not None
        assert entry.skills == [{"slug": "a"}]
    # exactly one caller has computed=True (the computer); the rest are False
    computed_flags = [computed for _, computed in results]
    assert sum(computed_flags) == 1, f"one computer, got {sum(computed_flags)}"
