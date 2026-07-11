"""Stale-while-revalidate unit tests for the metasearch hot-query cache
(metasearch_0710 §7.5 latency fix, 2026-07-11).

These drive the fresh → stale → hard-expired state machine and the single-flight
background refresh with SUB-SECOND ttl/grace so the SWR path is exercised
deterministically (the prod defaults are 300s TTL + 600s grace; a live test that
waited those out would be untenable). The §7.5 acceptance load test proves the
end-to-end p95; these prove the mechanism.
"""

from __future__ import annotations

import threading
import time

from app.services.metasearch_cache import CacheEntry, HotQueryCache


def _entry(age_offset: float, ttl: float, grace: float) -> CacheEntry:
    """Build an entry whose cached_at is `age_offset` seconds in the past."""
    return CacheEntry(
        skills=[{"slug": "x"}],
        sources_ok=["recipes"],
        sources_degraded=[],
        sources_failed=[],
        cached_at=time.monotonic() - age_offset,
        ttl_s=ttl,
        stale_grace_s=grace,
    )


# ── State machine: fresh / stale / expired ────────────────────────────────────


def test_entry_fresh_within_ttl():
    e = _entry(age_offset=0.0, ttl=10.0, grace=10.0)
    assert e.fresh is True
    assert e.stale is False
    assert e.expired is False


def test_entry_stale_past_ttl_within_grace():
    e = _entry(age_offset=5.0, ttl=1.0, grace=10.0)  # age 5s, ttl 1s, grace 10s
    assert e.fresh is False
    assert e.stale is True
    assert e.expired is False


def test_entry_hard_expired_past_ttl_plus_grace():
    e = _entry(age_offset=20.0, ttl=1.0, grace=1.0)  # age 20s > 1+1
    assert e.fresh is False
    assert e.stale is False
    assert e.expired is True


def test_response_meta_flags_stale():
    fresh = _entry(0.0, ttl=10.0, grace=10.0)
    stale = _entry(5.0, ttl=1.0, grace=10.0)
    assert fresh.to_response_meta()["cache_stale"] is False
    assert stale.to_response_meta()["cache_stale"] is True


# ── get_entry state classification + counters ─────────────────────────────────


def test_get_entry_states_and_counters():
    c = HotQueryCache(ttl_s=0.3, max_entries=10, stale_grace_s=5.0)
    src = ("skills-sh",)
    c.put("browser", src, [{"slug": "a"}], sources_ok=["skills-sh"])

    # Fresh immediately.
    entry, state = c.get_entry("browser", src)
    assert state == "fresh" and entry is not None

    # After TTL but within grace → stale.
    time.sleep(0.35)
    entry, state = c.get_entry("browser", src)
    assert state == "stale" and entry is not None
    assert c.stats()["stale_serves"] == 1

    # A stale serve still counts as a hit (fast cached response delivered).
    assert c.stats()["hits"] == 2  # fresh + stale


def test_get_strict_returns_none_for_stale():
    """The strict `get()` accessor treats stale as a miss (backward-compat for
    the P5 tests that assert cache_hit off a fresh entry)."""
    c = HotQueryCache(ttl_s=0.3, max_entries=10, stale_grace_s=5.0)
    src = ("skills-sh",)
    c.put("browser", src, [{"slug": "a"}])
    assert c.get("browser", src) is not None  # fresh
    time.sleep(0.35)
    assert c.get("browser", src) is None  # stale → strict get == None


def test_hard_expired_is_miss_and_evicts():
    c = HotQueryCache(ttl_s=0.2, max_entries=10, stale_grace_s=0.2)
    src = ("skills-sh",)
    c.put("browser", src, [{"slug": "a"}])
    time.sleep(0.5)  # past ttl(0.2) + grace(0.2)
    entry, state = c.get_entry("browser", src)
    assert state == "miss" and entry is None
    # Evicted from the store.
    assert c.stats()["entries"] == 0


# ── Single-flight background refresh on the stale path ────────────────────────


def test_stale_serve_fires_exactly_one_background_refresh():
    c = HotQueryCache(ttl_s=0.2, max_entries=10, stale_grace_s=5.0)
    src = ("skills-sh",)
    c.put("browser", src, [{"slug": "old"}], sources_ok=["skills-sh"])

    refresh_calls = []
    refresh_gate = threading.Event()

    def _refresh():
        refresh_calls.append(1)
        refresh_gate.wait(timeout=2.0)  # hold the refresh open so we can race it
        return [{"slug": "new"}], ["skills-sh"], []

    time.sleep(0.3)  # entry now stale

    # Fire N concurrent stale-serves; exactly ONE refresh must start.
    results = []

    def _hit():
        entry, computed = c.get_or_compute(("browser", src), _refresh, refresh_fn=_refresh)
        results.append((entry, computed))

    threads = [threading.Thread(target=_hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)

    # All 8 got the STALE entry immediately (computed=False), none blocked on compute.
    assert len(results) == 8
    assert all(computed is False for _, computed in results)
    assert all(entry is not None and entry.skills == [{"slug": "old"}] for entry, _ in results)
    # Exactly one background refresh started despite 8 concurrent stale-serves.
    assert len(refresh_calls) == 1

    # Release the refresh; the cache should now hold the new value.
    refresh_gate.set()
    # Wait for the daemon refresh to complete + store.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        entry, state = c.get_entry("browser", src)
        if entry is not None and entry.skills == [{"slug": "new"}]:
            break
        time.sleep(0.02)
    entry, _ = c.get_entry("browser", src)
    assert entry is not None and entry.skills == [{"slug": "new"}]


def test_refresh_failure_leaves_stale_entry_intact():
    """A failed background refresh must NOT poison the cache — the stale entry
    stays until it hard-expires, then the next request recomputes."""
    c = HotQueryCache(ttl_s=0.2, max_entries=10, stale_grace_s=5.0)
    src = ("skills-sh",)
    c.put("browser", src, [{"slug": "old"}])

    def _boom():
        raise RuntimeError("upstream down")

    time.sleep(0.3)  # stale
    entry, computed = c.get_or_compute(("browser", src), _boom, refresh_fn=_boom)
    assert computed is False
    assert entry is not None and entry.skills == [{"slug": "old"}]  # stale served

    # Give the failing refresh a moment; the old entry must survive.
    time.sleep(0.3)
    entry2, state = c.get_entry("browser", src)
    assert entry2 is not None and entry2.skills == [{"slug": "old"}]


def test_hard_miss_computes_synchronously():
    """A true cold miss (no entry) runs compute_fn synchronously and returns
    computed=True."""
    c = HotQueryCache(ttl_s=5.0, max_entries=10, stale_grace_s=5.0)
    src = ("skills-sh",)

    def _compute():
        return [{"slug": "fresh"}], ["skills-sh"], []

    entry, computed = c.get_or_compute(("novel-query", src), _compute)
    assert computed is True
    assert entry is not None and entry.skills == [{"slug": "fresh"}]


# ── CAS: a slow SWR refresh must NOT clobber a newer hard-miss recompute ───────


def test_slow_refresh_does_not_overwrite_newer_hard_miss(monkeypatch):
    """Council MUST-FIX (2026-07-11), reproduced by the reviewer:

    1. entry goes stale → background refresh starts computing 'bg' (held open)
    2. entry ages past ttl+grace while bg still running → next caller hard-misses,
       synchronously computes+stores 'sync' (newer, higher seq)
    3. the slow bg refresh finishes → its compare-and-swap MUST fail (the entry it
       started from was replaced) so it does NOT overwrite 'sync' with 'bg'.

    Without the seq/CAS guard the final cache would be 'bg' (the reviewer's
    reproduction). With it, the final cache must be 'sync'.
    """
    c = HotQueryCache(ttl_s=0.1, max_entries=10, stale_grace_s=0.1)
    src = ("skills-sh",)
    c.put("browser", src, [{"slug": "old"}], sources_ok=["skills-sh"])

    bg_started = threading.Event()
    bg_release = threading.Event()

    def _bg_refresh():
        bg_started.set()
        bg_release.wait(timeout=3.0)  # hold the refresh open past the entry's hard-expiry
        return [{"slug": "bg"}], ["skills-sh"], []

    # Serve stale → fires the (held) background refresh.
    time.sleep(0.12)  # past ttl(0.1), within grace(0.1)
    entry, computed = c.get_or_compute(("browser", src), lambda: None, refresh_fn=_bg_refresh)
    assert computed is False
    assert entry is not None and entry.skills == [{"slug": "old"}]
    assert bg_started.wait(timeout=1.0)

    # Let the OLD entry age past ttl+grace while the bg refresh is still held.
    time.sleep(0.15)  # now age > 0.1 + 0.1 → hard-expired

    # A hard miss recomputes synchronously and stores the NEWER 'sync' value.
    def _sync():
        return [{"slug": "sync"}], ["skills-sh"], []

    entry2, computed2 = c.get_or_compute(("browser", src), _sync, refresh_fn=_bg_refresh)
    assert computed2 is True
    assert entry2 is not None and entry2.skills == [{"slug": "sync"}]

    # Release the slow bg refresh; its CAS must fail (entry moved on).
    bg_release.set()
    # Give the daemon a moment to attempt (and drop) its write.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        e, _ = c.get_entry("browser", src, _count=False)
        # If the bug were present, e.skills would flip to 'bg'. Poll a bit to
        # give the racing thread every chance to clobber, then assert it didn't.
        time.sleep(0.05)
        if not c._refreshing:  # refresh finished
            break

    final, _ = c.get_entry("browser", src, _count=False)
    assert final is not None
    assert final.skills == [{"slug": "sync"}], (
        f"stale refresh clobbered the newer hard-miss result: got {final.skills}"
    )


def test_put_if_current_cas_semantics():
    """Unit-level CAS: put_if_current lands only when the expected seq matches."""
    c = HotQueryCache(ttl_s=5.0, max_entries=10, stale_grace_s=5.0)
    src = ("skills-sh",)
    seq0 = c.put("browser", src, [{"slug": "v0"}])

    # Matching seq → lands.
    assert c.put_if_current("browser", src, [{"slug": "v1"}], expected_seq=seq0) is True
    entry, _ = c.get_entry("browser", src, _count=False)
    assert entry.skills == [{"slug": "v1"}]

    # Stale seq (seq0 is now behind) → rejected, entry unchanged.
    assert c.put_if_current("browser", src, [{"slug": "vX"}], expected_seq=seq0) is False
    entry, _ = c.get_entry("browser", src, _count=False)
    assert entry.skills == [{"slug": "v1"}]

    # Missing entry → CAS fails (nothing to compare against).
    c.invalidate()
    assert c.put_if_current("novel", src, [{"slug": "y"}], expected_seq=1) is False


def test_strict_get_does_not_inflate_hit_stats_on_stale():
    """Council SHOULD (2026-07-11): strict get() returns None for a stale entry
    and must NOT record a hit (it delegates with _count=False)."""
    c = HotQueryCache(ttl_s=0.1, max_entries=10, stale_grace_s=5.0)
    src = ("skills-sh",)
    c.put("browser", src, [{"slug": "a"}])
    c.reset_stats()
    time.sleep(0.15)  # stale
    assert c.get("browser", src) is None  # strict get → miss semantics
    s = c.stats()
    assert s["hits"] == 0, f"strict get() on stale must not count a hit, stats={s}"
    assert s["stale_serves"] == 0
