"""Hot-query cache for metasearch fan-out (metasearch_0710 P5 / §7).

THE scale workhorse. The plan (§7.5) is explicit: "the cache is doing the real
work; the token bucket is the seatbelt." Popular queries ("browser", "scraping",
"email") collapse to ONE upstream call per TTL window — at 300 searches/min with
an 80%+ cache hit rate, upstream QPS to each source stays in low single digits,
comfortably under ClawHub's 3000/window.

Design (thin, not fat — §7):
- In-process LRU keyed by (normalized_query, sorted(sources)). The TTL is the
  freshness control; the LRU cap bounds memory.
- Stores the merged+ranked UnifiedSkill list + source-health metadata so a cache
  hit returns the EXACT same response shape as a live fan-out (the §5 render
  contract + funnel telemetry apply identically).
- Cache miss → live fan-out → store (TTL). Cache hit → return cached + mark
  ``cache_hit=True`` in the response metadata (the funnel measures hit rate).
- NO background warming — §7 hard rule: "we only call sources when a user
  searches." The cache is populated on-demand, never pre-walked.

Redis-backed fleet-wide cache (P5+): the in-process cache is per-worker. A
Redis-backed shared cache would collapse across workers + instances, but the plan
defers Redis to P5+ because the per-worker cache + the human-bounded search load
already fit the rate limits at the 1000-fleet-runner target. The interface
(``get``/``put``) is designed so a Redis backend can drop in behind the same API.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# §7: "thin, not fat." The cache holds the merged result for a (query, sources)
# pair for a short TTL. Defaults are conservative — tuned to keep popular queries
# fresh enough for discovery, stale enough to collapse burst traffic.
_DEFAULT_TTL_S = 300  # 5 min — the plan's §7 "5–15 min" range, floor
_DEFAULT_MAX_ENTRIES = 500  # bounded LRU; at ~2KB/entry this is ~1MB
# §7.5 latency: stale-while-revalidate grace. Past the TTL but within this
# window, a hit is served STALE (fast) while a background thread refreshes the
# entry. This collapses the expiry-boundary miss — the single request that used
# to pay the full cold fan-out at each TTL rollover — into a fast stale-serve,
# which is what drives the p95 tail below the 500ms acceptance gate. The refresh
# runs on its OWN DB session (the request session is closed by then).
_DEFAULT_STALE_GRACE_S = 600  # serve stale up to 10 min past TTL while refreshing


@dataclass
class CacheEntry:
    """A cached fan-out result. Stores the merged list + health so a hit returns
    the exact same response shape as a live call."""

    skills: list[dict[str, Any]]
    sources_ok: list[str]
    sources_degraded: list[str]
    sources_failed: list[str]
    cached_at: float
    ttl_s: float
    stale_grace_s: float = 0.0
    seq: int = 0  # monotonic write-generation; the CAS token for SWR refreshes

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.cached_at

    @property
    def fresh(self) -> bool:
        """Within TTL — serve directly, no refresh needed."""
        return self.age_s <= self.ttl_s

    @property
    def stale(self) -> bool:
        """Past TTL but within the stale-while-revalidate grace window — serve
        this entry immediately AND trigger a background refresh."""
        age = self.age_s
        return self.ttl_s < age <= (self.ttl_s + self.stale_grace_s)

    @property
    def expired(self) -> bool:
        """Past TTL + grace — must not be served; treat as a hard miss."""
        return self.age_s > (self.ttl_s + self.stale_grace_s)

    def to_response_meta(self) -> dict[str, Any]:
        """Metadata added to the metasearch response on a cache hit."""
        age_s = self.age_s
        return {
            "cache_hit": True,
            "cache_age_s": round(age_s, 1),
            "cache_ttl_s": self.ttl_s,
            "cache_stale": self.stale,
            "sources_ok": self.sources_ok,
            "sources_degraded": self.sources_degraded,
            "sources_failed": self.sources_failed,
        }


@dataclass
class HotQueryCache:
    """In-process LRU + TTL cache for metasearch fan-out results.

    Thread-safe via a single instance-level lock. Designed for a Redis backend to
    drop behind the same ``get``/``put`` interface without changing callers.
    """

    ttl_s: float = _DEFAULT_TTL_S
    max_entries: int = _DEFAULT_MAX_ENTRIES
    stale_grace_s: float = _DEFAULT_STALE_GRACE_S
    _store: "OrderedDict[str, CacheEntry]" = field(default_factory=OrderedDict)
    _hits: int = 0
    _misses: int = 0
    _stale_serves: int = 0
    _lock: Any = None  # lazily initialized (threading.Lock isn't a dataclass field)
    _inflight: dict[str, Any] = field(default_factory=dict)  # key → Event for single-flight
    _refreshing: set[str] = field(default_factory=set)  # keys with an in-progress SWR refresh
    _seq_counter: int = 0  # monotonic write-generation source (CAS token for SWR)

    def __post_init__(self):
        object.__setattr__(self, "_lock", threading.Lock())

    def _key(self, query: str, sources: tuple[str, ...]) -> str:
        # Normalize: lowercase, collapse whitespace, strip. This is what makes
        # "Browser " and "browser" share a cache entry (§7 "popular queries
        # collapse").
        normalized = " ".join(query.lower().split())
        return f"{normalized}|{','.join(sorted(sources))}"

    def get(self, query: str, sources: tuple[str, ...]) -> CacheEntry | None:
        """Return a FRESH (within-TTL) cached entry, or None. LRU-promotes on hit.

        This is the strict freshness accessor: a stale (past-TTL, within-grace)
        entry returns None here so direct callers get miss semantics. The SWR
        serve-stale path lives in ``get_entry`` / ``get_or_compute``.

        Does NOT touch hit/miss counters (``_count=False``): the request-path SWR
        accounting is owned by ``get_or_compute`` via ``get_entry``. A strict
        ``get()`` that returned None for a stale entry while ALSO recording a hit
        would corrupt the §7.5 hit-rate telemetry (council SHOULD, 2026-07-11).
        """
        entry, state = self.get_entry(query, sources, _count=False)
        if entry is not None and state == "fresh":
            return entry
        return None

    def get_entry(
        self, query: str, sources: tuple[str, ...], *, _count: bool = True
    ) -> tuple[CacheEntry | None, str]:
        """Return (entry, state) where state ∈ {"fresh", "stale", "miss"}.

        - fresh: within TTL — serve, no refresh.
        - stale: past TTL, within grace — serve THIS entry (fast) and the caller
          should trigger a background refresh (stale-while-revalidate).
        - miss: no entry, or past TTL+grace (hard-expired, evicted here).

        Hit/miss counters (only when ``_count`` — the request path): a fresh OR
        stale serve counts as a hit (the user got a fast cached response); only a
        true miss increments misses. This mirrors what the §7.5 load test measures
        (``cache_hit`` in the response). Strict ``get()`` passes ``_count=False``
        so its stale→None discard does not inflate the hit counter.
        """
        with self._lock:
            key = self._key(query, sources)
            entry = self._store.get(key)
            if entry is None:
                if _count:
                    self._misses += 1
                return None, "miss"
            if entry.expired:
                # Hard-expired (past TTL + grace) — evict, count as miss.
                self._store.pop(key, None)
                if _count:
                    self._misses += 1
                return None, "miss"
            # Fresh or stale: LRU-promote and (on the request path) count a hit.
            self._store.move_to_end(key)
            if entry.stale:
                if _count:
                    self._hits += 1
                    self._stale_serves += 1
                return entry, "stale"
            if _count:
                self._hits += 1
            return entry, "fresh"

    def put(
        self,
        query: str,
        sources: tuple[str, ...],
        skills: list[dict[str, Any]],
        *,
        sources_ok: list[str] | None = None,
        sources_degraded: list[str] | None = None,
        sources_failed: list[str] | None = None,
    ) -> int:
        """Store a fan-out result unconditionally (foreground write — always wins;
        a freshly-computed result is by definition the newest). Returns the seq
        stamped on the new entry. Evicts the LRU entry if at capacity."""
        with self._lock:
            return self._store_locked(
                query,
                sources,
                skills,
                sources_ok=sources_ok,
                sources_degraded=sources_degraded,
                sources_failed=sources_failed,
            )

    def put_if_current(
        self,
        query: str,
        sources: tuple[str, ...],
        skills: list[dict[str, Any]],
        *,
        expected_seq: int,
        sources_ok: list[str] | None = None,
        sources_degraded: list[str] | None = None,
        sources_failed: list[str] | None = None,
    ) -> bool:
        """Compare-and-swap store for the BACKGROUND SWR refresh. Only overwrites
        if the entry currently in the store is STILL the one the refresh started
        from (its seq == ``expected_seq``). Returns True iff the write landed.

        This closes the overwrite race (council MUST-FIX, 2026-07-11): if the
        stale entry aged past TTL+grace while this refresh was running, a hard-miss
        recompute (or a newer refresh) already stored a fresher entry with a higher
        seq — the CAS then fails and this stale refresh result is DISCARDED rather
        than clobbering the newer value. A missing entry (evicted) also fails the
        CAS: the refresh result is dropped, and the next request recomputes.
        """
        with self._lock:
            key = self._key(query, sources)
            current = self._store.get(key)
            if current is None or current.seq != expected_seq:
                return False
            self._store_locked(
                query,
                sources,
                skills,
                sources_ok=sources_ok,
                sources_degraded=sources_degraded,
                sources_failed=sources_failed,
            )
            return True

    def _store_locked(
        self,
        query: str,
        sources: tuple[str, ...],
        skills: list[dict[str, Any]],
        *,
        sources_ok: list[str] | None = None,
        sources_degraded: list[str] | None = None,
        sources_failed: list[str] | None = None,
    ) -> int:
        """Write an entry with a fresh monotonic seq. MUST be called under
        ``self._lock``. Returns the assigned seq."""
        key = self._key(query, sources)
        self._seq_counter += 1
        seq = self._seq_counter
        self._store[key] = CacheEntry(
            skills=skills,
            sources_ok=sources_ok or [],
            sources_degraded=sources_degraded or [],
            sources_failed=sources_failed or [],
            cached_at=time.monotonic(),
            ttl_s=self.ttl_s,
            stale_grace_s=self.stale_grace_s,
            seq=seq,
        )
        self._store.move_to_end(key)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)  # FIFO eviction = LRU oldest
        return seq

    def stats(self) -> dict[str, Any]:
        """Hit-rate telemetry for the §7.5 acceptance test (80%+ target) and the
        ``federation-funnel-alive`` predicate."""
        total = self._hits + self._misses
        return {
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "stale_serves": self._stale_serves,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "ttl_s": self.ttl_s,
            "stale_grace_s": self.stale_grace_s,
            "max_entries": self.max_entries,
        }

    def invalidate(self, query: str | None = None) -> int:
        """Invalidate entries. No query → clear all (admin/test). Returns count."""
        with self._lock:
            if query is None:
                n = len(self._store)
                self._store.clear()
                return n
            # Invalidate all source-sets for this query (prefix match on the key).
            prefix = f"{' '.join(query.lower().split())}|"
            keys_to_drop = [k for k in self._store if k.startswith(prefix)]
            for k in keys_to_drop:
                self._store.pop(k, None)
            return len(keys_to_drop)

    def reset_stats(self) -> None:
        """Reset hit/miss counters (admin/test). Entries are NOT cleared — use
        invalidate(None) for that. Council SHOULD 5: test isolation."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._stale_serves = 0

    def get_or_compute(
        self,
        key_parts: tuple[str, tuple[str, ...]],
        compute_fn: "Any",
        refresh_fn: "Any" = None,
    ) -> tuple[CacheEntry | None, bool]:
        """Single-flight + stale-while-revalidate.

        Fast paths:
          - FRESH hit → return (entry, False) immediately.
          - STALE hit (past TTL, within grace) → return the stale (entry, False)
            immediately AND fire ONE background refresh for this key (guarded by
            ``_refreshing`` so N concurrent stale-serves spawn exactly one
            refresh). The user pays ZERO fan-out latency at the TTL boundary —
            this is the §7.5 p95 fix.

        Slow path (hard miss — no entry or past TTL+grace):
          - Single-flight compute: the first caller runs ``compute_fn`` ONCE;
            concurrent callers for the same key wait on its Event, then read the
            cached result (council MUST: thundering herd).

        ``refresh_fn`` (optional) is the callable used for the BACKGROUND stale
        refresh; it MUST be self-contained w.r.t. resources (open its own DB
        session) because it runs in a daemon thread after the originating
        request's session is closed. When omitted, ``compute_fn`` is reused (safe
        only if ``compute_fn`` is itself resource-self-contained).

        Returns (entry, computed). ``computed=True`` iff THIS caller ran
        ``compute_fn`` synchronously (a hard-miss compute); a fresh hit, a
        stale-serve, and a single-flight waiter all return ``computed=False``.
        """
        query, sources = key_parts
        key = self._key(query, sources)

        # Fast path: fresh or stale hit (no single-flight lock contention).
        entry, state = self.get_entry(query, sources)
        if state == "fresh":
            return entry, False
        if state == "stale":
            # SWR: serve stale NOW, refresh in the background. Capture the served
            # entry's seq so the refresh does a compare-and-swap store — it must
            # NOT clobber a newer value written by a later hard-miss recompute if
            # this refresh outlives the entry's hard-expiry (council MUST-FIX).
            self._maybe_refresh(
                key,
                query,
                sources,
                refresh_fn or compute_fn,
                expected_seq=entry.seq if entry is not None else None,
            )
            return entry, False

        # Hard miss: acquire or create an in-flight slot for this key.
        with self._lock:
            existing = self._store.get(key)
            if existing is not None and not existing.expired:
                # Raced with another writer between get_entry and here.
                return existing, False
            event = self._inflight.get(key)
            if event is None:
                # First caller for this key — we compute.
                event = threading.Event()
                self._inflight[key] = event
                is_computer = True
            else:
                is_computer = False

        if is_computer:
            try:
                self._run_and_store(query, sources, compute_fn)
            except Exception:  # noqa: BLE001
                # Rationale: a failed compute must not be cached; concurrent waiters
                # see no entry and retry on their next request.
                logger.warning("cache compute failed for %s", key, exc_info=True)
                # Council R3: set the Event BEFORE popping _inflight so a new caller
                # arriving in the gap doesn't become a second computer (pop-before-set race).
                event.set()
                with self._lock:
                    self._inflight.pop(key, None)
                return None, True
            # Success: set Event first (release waiters), then clean up _inflight.
            event.set()
            with self._lock:
                self._inflight.pop(key, None)
            entry = self.get(query, sources)
            return entry, True
        else:
            # Concurrent waiter — the computer populated the cache; read it.
            # Council R3: return computed=False (we didn't compute — we waited).
            event.wait(timeout=30.0)
            entry = self.get(query, sources)
            return entry, False

    def _run_and_store(
        self, query: str, sources: tuple[str, ...], compute_fn: "Any", *, expected_seq: int | None = None
    ) -> None:
        """Call ``compute_fn`` and store its result.

        - Foreground hard-miss (``expected_seq is None``): unconditional ``put`` —
          a freshly-computed result is the newest, it always wins.
        - Background SWR refresh (``expected_seq`` set): compare-and-swap via
          ``put_if_current`` — the write lands ONLY if the entry we started from
          is still current. If a newer hard-miss recompute replaced it while this
          refresh ran, the CAS fails and this (now-stale) result is discarded
          instead of clobbering the newer value (council MUST-FIX, 2026-07-11).
        """
        result = compute_fn()
        # compute_fn returns (skills, sources_ok, sources_degraded) or just skills.
        if isinstance(result, tuple) and len(result) == 3:
            skills, ok, degraded = result
        else:
            skills, ok, degraded = result, None, None

        if expected_seq is None:
            self.put(query, sources, skills, sources_ok=ok, sources_degraded=degraded)
        else:
            landed = self.put_if_current(
                query,
                sources,
                skills,
                expected_seq=expected_seq,
                sources_ok=ok,
                sources_degraded=degraded,
            )
            if not landed:
                logger.debug(
                    "metasearch SWR refresh for %s|%s discarded (entry moved on; CAS miss)",
                    query,
                    sources,
                )

    def _maybe_refresh(
        self,
        key: str,
        query: str,
        sources: tuple[str, ...],
        compute_fn: "Any",
        *,
        expected_seq: int | None = None,
    ) -> bool:
        """Fire a SINGLE background refresh for a stale key. Returns True iff this
        call started the refresh (i.e. won the ``_refreshing`` guard). Concurrent
        stale-serves for the same key are no-ops — exactly one refresh runs.

        ``expected_seq`` is the seq of the stale entry that was served; the refresh
        stores via compare-and-swap so it cannot overwrite a newer entry written
        by a hard-miss recompute if this refresh outlives the entry's hard-expiry.

        The refresh thread is a daemon so it never blocks process shutdown; on
        failure the stale entry simply remains until it hard-expires (grace
        window) and the next request does a synchronous compute.
        """
        with self._lock:
            if key in self._refreshing:
                return False
            self._refreshing.add(key)

        def _refresh() -> None:
            try:
                self._run_and_store(query, sources, compute_fn, expected_seq=expected_seq)
            except Exception:  # noqa: BLE001
                # Rationale: a failed background refresh must not crash the worker
                # and must not poison the cache — the stale entry stays until it
                # hard-expires, then a request recomputes synchronously.
                logger.warning("metasearch SWR refresh failed for %s", key, exc_info=True)
            finally:
                with self._lock:
                    self._refreshing.discard(key)

        threading.Thread(target=_refresh, name="metasearch-swr-refresh", daemon=True).start()
        return True


# Module-level singleton (per-worker). The metasearch route uses this instance.
# A Redis-backed shared cache (P5+) would replace this with a Redis-backed
# implementation behind the same interface.
_cache = HotQueryCache()


def get_cache() -> HotQueryCache:
    """Return the module-level cache singleton."""
    return _cache
