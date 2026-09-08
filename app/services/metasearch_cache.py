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

Redis-backed fleet-wide cache (unisearch_0709 P1): the L2 predicted in this
docstring is now here, dropped in behind the SAME ``get``/``put`` interface.
L1 is the in-process LRU below; L2 is Redis (``app/services/metasearch_cache_l2.py``
owns the wire format). A result computed by worker A is now readable by every
other worker, which is what makes the MCP cache-ONLY reader (P2) able to answer
at all — it never fans out, so a per-worker cache made it answer "cold" for a
query the REST route had just warmed one worker over.

Four rules the L2 adds, none of which the callers see:
- **Absolute epoch.** ``computed_at`` is unix seconds; ``time.monotonic()`` is
  per-process and lies the moment a value crosses a process boundary.
- **Shared seq.** The write generation comes from a Redis ``INCR``, so worker A's
  slow stale-refresh compare-and-sets against worker B's newer entry instead of
  clobbering it.
- **Sanitised writes.** ``put()`` field-caps and version-tags before sharing —
  an unsanitised shared cache is a fleet-wide poisoning surface.
- **Honest degradation.** Redis unreachable (including the 30s ``get_redis()``
  backoff window) → L1-only and state ``degraded``. Never an exception to the
  caller, never a blocking wait, never an empty-but-"fresh" answer.

An instance with no ``l2`` injected is exactly the pre-P1 in-process cache.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from app.services.metasearch_cache_l2 import RedisL2, encode_payload, redis_key, sanitize_skills
from app.services.metasearch_cache_swr import SingleFlightSWRMixin

logger = logging.getLogger(__name__)

# §7: "thin, not fat." The cache holds the merged result for a (query, sources)
# pair for a short TTL. Defaults are conservative — tuned to keep popular queries
# fresh enough for discovery, stale enough to collapse burst traffic.
_DEFAULT_TTL_S = 300  # 5 min — the plan's §7 "5–15 min" range, floor
# L1 cap (unisearch_0709 P1): 64. Small on purpose — L1 is now a latency shield
# in front of the shared Redis tier, not the whole cache, so it only needs to
# hold the working set of one worker's hot queries. Redis evicts by TTL.
_DEFAULT_MAX_ENTRIES = 64  # bounded LRU; at ~2KB/entry this is ~128KB
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
    computed_at: float  # UNIX EPOCH seconds — never time.monotonic(): this
    # value is read by other processes (P1), and a monotonic stamp is
    # per-process (it reads as ~uptime, i.e. ancient, anywhere else).
    ttl_s: float
    stale_grace_s: float = 0.0
    seq: int = 0  # SHARED write-generation (Redis INCR); the CAS token for SWR

    @property
    def age_s(self) -> float:
        # max(0): another worker's clock may be marginally ahead of ours.
        return max(0.0, time.time() - self.computed_at)

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


class CacheLookup(NamedTuple):
    """The result of a cache-ONLY read: a payload plus an EXPLICIT state.

    ``state`` is one of:
      - ``fresh``    — within TTL. Serve it.
      - ``stale``    — past TTL, inside the grace window. Serve it AND refresh.
      - ``miss``     — nothing cached (or hard-expired). A normal, fast answer.
      - ``degraded`` — the shared tier is unreachable, so freshness cannot be
        confirmed fleet-wide. ``entry`` may still carry an L1 payload; if it
        does, it is servable — flagged honestly rather than dressed up as fresh.

    A NamedTuple so the long-standing ``entry, state = get_entry(...)`` callers
    keep working unchanged while new callers (the P2 MCP reader) can use the
    named fields.
    """

    entry: "CacheEntry | None"
    state: str

    @property
    def payload(self) -> "CacheEntry | None":
        """Alias for ``entry`` — reads better at the MCP call site."""
        return self.entry

    @property
    def servable(self) -> bool:
        """True iff there is something to return to the user right now."""
        return self.entry is not None


@dataclass
class HotQueryCache(SingleFlightSWRMixin):
    """In-process LRU + TTL cache for metasearch fan-out results.

    Thread-safe via a single instance-level lock. Designed for a Redis backend to
    drop behind the same ``get``/``put`` interface without changing callers.
    """

    ttl_s: float = _DEFAULT_TTL_S
    max_entries: int = _DEFAULT_MAX_ENTRIES
    stale_grace_s: float = _DEFAULT_STALE_GRACE_S
    # L2. None → pure in-process cache (the pre-P1 behaviour, and what unit
    # tests get by default so they never depend on a live Redis).
    l2: RedisL2 | None = None
    _store: "OrderedDict[str, CacheEntry]" = field(default_factory=OrderedDict)
    _hits: int = 0
    _misses: int = 0
    _stale_serves: int = 0
    _degraded_serves: int = 0
    # Last-known health of the shared tier. Sticky between L2 interactions so an
    # L1 fast-path hit is still reported honestly while Redis is down.
    _l2_down: bool = False
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

    # ── L2 plumbing ──────────────────────────────────────────────────────────

    def _mark_l2(self, reachable: bool) -> None:
        """Record the outcome of an L2 interaction (drives the ``degraded`` state)."""
        self._l2_down = not reachable

    def _entry_from_payload(self, payload: dict[str, Any]) -> CacheEntry:
        """Rebuild an entry from a shared payload. ``sources_failed`` is not part
        of the shared schema (the fan-out never populates it) so it hydrates
        empty — L1-only field, documented rather than silently lossy."""
        return CacheEntry(
            skills=payload["skills"],
            sources_ok=payload["sources_ok"],
            sources_degraded=payload["sources_degraded"],
            sources_failed=[],
            computed_at=payload["computed_at"],
            ttl_s=payload["ttl_s"],
            stale_grace_s=payload["stale_grace_s"],
            seq=payload["seq"],
        )

    def _read_l2(self, key: str) -> tuple[bool, CacheEntry | None]:
        """Read the shared tier. Returns ``(reachable, entry)``."""
        if self.l2 is None:
            return True, None
        reachable, payload = self.l2.read(redis_key(key))
        self._mark_l2(reachable)
        if not reachable or payload is None:
            return reachable, None
        return True, self._entry_from_payload(payload)

    def _write_l2(self, key: str, entry: CacheEntry, *, expected_seq: int | None) -> tuple[bool, bool]:
        """Write through to the shared tier. Returns ``(reachable, landed)``."""
        if self.l2 is None:
            return True, False
        payload = encode_payload(
            skills=entry.skills,
            sources_ok=entry.sources_ok,
            sources_degraded=entry.sources_degraded,
            computed_at=entry.computed_at,
            ttl_s=entry.ttl_s,
            stale_grace_s=entry.stale_grace_s,
            seq=entry.seq,
        )
        reachable, landed = self.l2.write(
            redis_key(key),
            payload,
            seq=entry.seq,
            # Redis key TTL = ttl_s + stale_grace_s: the entry lives exactly as
            # long as it is servable, then Redis expires it (TTL, not LRU).
            ttl_s=entry.ttl_s + entry.stale_grace_s,
            expected_seq=expected_seq,
        )
        self._mark_l2(reachable)
        return reachable, landed

    def get(self, query: str, sources: tuple[str, ...]) -> CacheEntry | None:
        """Return a FRESH (within-TTL) cached entry, or None. LRU-promotes on hit.

        Strict freshness accessor: a stale (past-TTL, within-grace) entry returns
        None here so direct callers get miss semantics. The SWR serve-stale path
        lives in ``get_entry`` / ``get_or_compute``.

        Accounting (council SHOULD, 2026-07-11): counts a FRESH serve as a hit and
        a true miss as a miss — matching the long-standing P5 stats contract
        (test_hit_rate_stats). But a STALE entry (returned as None here) is counted
        as a MISS, not a hit, so the strict path's hit-rate telemetry stays honest.
        It suppresses ``get_entry``'s own counting (``_count=False``) and records
        the outcome itself to avoid double counting.

        Freshness here is judged on the ENTRY (absolute-epoch age vs TTL), not on
        the lookup state, so a Redis outage degrades the flag without forcing the
        REST route to recompute a perfectly fresh entry on every request.
        """
        lookup = self.get_entry(query, sources, _count=False)
        with self._lock:
            if lookup.entry is not None and lookup.entry.fresh:
                self._hits += 1
                return lookup.entry
            # stale (returned as None to strict callers) or miss → count a miss.
            self._misses += 1
            return None

    def get_entry(self, query: str, sources: tuple[str, ...], *, _count: bool = True) -> CacheLookup:
        """CACHE-ONLY read. Returns ``CacheLookup(entry, state)``.

        This is the reader the MCP search path imports (unisearch_0709 P2). It
        NEVER computes, NEVER fans out and NEVER blocks on upstream or on another
        worker: at worst it does one Redis GET and answers ``miss``. A miss is a
        normal, fast answer — the caller returns native results and flags the
        federated section honestly.

        States:
          - fresh: within TTL — serve, no refresh.
          - stale: past TTL, within grace — serve THIS entry (fast) and the
            caller should trigger a background refresh (stale-while-revalidate).
          - miss: nothing cached, or past TTL+grace (hard-expired, evicted here).
          - degraded: the shared tier is unreachable. Any L1 payload is still
            returned (L1-only mode); it is simply not claimed as fleet-fresh.

        Tier order: a FRESH L1 entry short-circuits (no Redis round-trip on the
        hot path). Otherwise the shared tier is consulted, and the higher ``seq``
        wins — another worker may have recomputed while ours went stale.

        Hit/miss counters (only when ``_count`` — the request path): any served
        payload counts as a hit; a non-servable answer counts as a miss.
        """
        key = self._key(query, sources)
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                self._store.move_to_end(key)
            l1_fresh = entry is not None and entry.fresh
            degraded = self._l2_down

        if not l1_fresh:
            reachable, remote = self._read_l2(key)
            degraded = not reachable
            if remote is not None and (entry is None or remote.seq > entry.seq):
                entry = remote
                with self._lock:
                    self._store[key] = remote
                    self._store.move_to_end(key)
                    self._trim_locked()

        if entry is not None and entry.expired:
            with self._lock:
                self._store.pop(key, None)
            entry = None

        if degraded:
            state = "degraded"
        elif entry is None:
            state = "miss"
        else:
            state = "stale" if entry.stale else "fresh"
        return self._record(CacheLookup(entry, state), _count=_count)

    def _record(self, lookup: CacheLookup, *, _count: bool) -> CacheLookup:
        """Count one lookup outcome and return it unchanged."""
        if not _count:
            return lookup
        with self._lock:
            if lookup.entry is None:
                self._misses += 1
            else:
                self._hits += 1
            if lookup.state == "stale":
                self._stale_serves += 1
            elif lookup.state == "degraded":
                self._degraded_serves += 1
        return lookup

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
        """Store a fan-out result (foreground write). Returns the seq stamped on
        the new entry. Evicts the LRU entry if L1 is at capacity.

        The payload is SANITISED, field-capped and version-tagged before it is
        shared (``metasearch_cache_l2.sanitize_skills`` / ``encode_payload``) —
        this write is read by every other worker, so an unsanitised row here is a
        fleet-wide blast radius, not a local one.

        The shared write is guarded by the same compare-and-set as a refresh, in
        "monotonic" mode: it lands only if it is strictly newer than what is
        already there. A freshly-computed result carries a freshly-INCR'd seq, so
        it is the newest by construction and effectively always wins; the guard
        exists so it can never go backwards.
        """
        entry = self._build_entry(
            skills,
            sources_ok=sources_ok,
            sources_degraded=sources_degraded,
            sources_failed=sources_failed,
        )
        key = self._key(query, sources)
        self._write_l2(key, entry, expected_seq=None)
        with self._lock:
            self._put_locked(key, entry)
        return entry.seq

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

        With a shared tier the CAS runs IN REDIS (one atomic script), because the
        entry this refresh started from may have been replaced by a DIFFERENT
        WORKER — a purely local compare would not see that write at all. When the
        shared tier is unreachable (or absent) the local compare is the fallback:
        L1-only mode, same semantics, one process's worth of truth.
        """
        entry = self._build_entry(
            skills,
            sources_ok=sources_ok,
            sources_degraded=sources_degraded,
            sources_failed=sources_failed,
        )
        key = self._key(query, sources)
        reachable, landed = self._write_l2(key, entry, expected_seq=expected_seq)
        if self.l2 is not None and reachable:
            if not landed:
                return False  # the shared entry moved on — drop this refresh
            with self._lock:
                self._put_locked(key, entry)
            return True
        with self._lock:
            current = self._store.get(key)
            if current is None or current.seq != expected_seq:
                return False
            self._put_locked(key, entry)
            return True

    def _build_entry(
        self,
        skills: list[dict[str, Any]],
        *,
        sources_ok: list[str] | None = None,
        sources_degraded: list[str] | None = None,
        sources_failed: list[str] | None = None,
    ) -> CacheEntry:
        """Sanitise a result and stamp it with a fresh SHARED seq + epoch."""
        return CacheEntry(
            skills=sanitize_skills(skills),
            sources_ok=sources_ok or [],
            sources_degraded=sources_degraded or [],
            sources_failed=sources_failed or [],
            computed_at=time.time(),
            ttl_s=self.ttl_s,
            stale_grace_s=self.stale_grace_s,
            seq=self._next_seq(),
        )

    def _next_seq(self) -> int:
        """Next write generation, from the SHARED Redis counter when available.

        A per-process counter cannot order writes across workers — worker A's
        "seq 3" and worker B's "seq 3" are unrelated numbers, so a compare-and-set
        against them is meaningless and the stale-refresh clobber is back. When
        Redis is unreachable we fall back to a local counter seeded from the
        highest shared value we have seen, so L1-only writes stay ordered
        locally and never spuriously outrank a real shared seq.
        """
        if self.l2 is not None:
            reachable, seq = self.l2.next_seq()
            self._mark_l2(reachable)
            if seq is not None:
                with self._lock:
                    self._seq_counter = max(self._seq_counter, seq)
                return seq
        with self._lock:
            self._seq_counter += 1
            return self._seq_counter

    def _put_locked(self, key: str, entry: CacheEntry) -> None:
        """Store an entry in L1 and trim. MUST be called under ``self._lock``."""
        self._store[key] = entry
        self._store.move_to_end(key)
        self._trim_locked()

    def _trim_locked(self) -> None:
        """Enforce the L1 cap. MUST be called under ``self._lock``."""
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)  # FIFO eviction = LRU oldest

    def stats(self) -> dict[str, Any]:
        """Hit-rate telemetry for the §7.5 acceptance test (80%+ target) and the
        ``federation-funnel-alive`` predicate."""
        total = self._hits + self._misses
        return {
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "stale_serves": self._stale_serves,
            "degraded_serves": self._degraded_serves,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "ttl_s": self.ttl_s,
            "stale_grace_s": self.stale_grace_s,
            "max_entries": self.max_entries,
            "l2_enabled": self.l2 is not None,
            "l2_degraded": self._l2_down,
        }

    def invalidate(self, query: str | None = None) -> int:
        """Invalidate entries in BOTH tiers. No query → clear all (admin/test).

        Returns the number of L1 entries dropped. The shared drop is best-effort:
        if Redis is unreachable the entries simply TTL out, which is the same
        outcome a moment later and is not worth failing an admin call over.
        """
        # All source-sets for a query share a key prefix; no query → everything.
        prefix = "" if query is None else f"{' '.join(query.lower().split())}|"
        with self._lock:
            dropped = [k for k in self._store if k.startswith(prefix)]
            for k in dropped:
                self._store.pop(k, None)
        if self.l2 is not None:
            # Prefix-scan, so entries written by OTHER workers (never present in
            # this process's L1) are invalidated too.
            self._mark_l2(self.l2.drop_matching(f"{prefix}*"))
        return len(dropped)

    def reset_stats(self) -> None:
        """Reset hit/miss counters (admin/test). Entries are NOT cleared — use
        invalidate(None) for that. Council SHOULD 5: test isolation."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._stale_serves = 0


def _default_l2() -> RedisL2 | None:
    """The shared tier for the module singleton, or None for L1-only.

    None when the shared cache is switched off, and when no ``REDIS_URL`` is
    configured at all — the documented zero-config self-host path. An
    unconfigured tier is NOT a degraded tier: a single-worker self-host with no
    Redis is working exactly as designed and must not report ``degraded``. Only
    a tier we expect to reach and cannot is degraded.
    """
    from app.config import settings

    if not settings.METASEARCH_SHARED_CACHE or not (settings.REDIS_URL or "").strip():
        return None
    return RedisL2()


# Module-level singleton (one per worker process). The metasearch REST route
# uses this instance; from unisearch_0709 P1 it writes through to the shared
# Redis tier, so what one worker computes, every worker (and the P2 MCP
# cache-only reader) can serve.
_cache = HotQueryCache(l2=_default_l2())


def get_cache() -> HotQueryCache:
    """Return the module-level cache singleton."""
    return _cache
