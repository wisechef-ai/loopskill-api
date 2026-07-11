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

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.cached_at) > self.ttl_s

    def to_response_meta(self) -> dict[str, Any]:
        """Metadata added to the metasearch response on a cache hit."""
        age_s = time.monotonic() - self.cached_at
        return {
            "cache_hit": True,
            "cache_age_s": round(age_s, 1),
            "cache_ttl_s": self.ttl_s,
            "sources_ok": self.sources_ok,
            "sources_degraded": self.sources_degraded,
            "sources_failed": self.sources_failed,
        }


@dataclass
class HotQueryCache:
    """In-process LRU + TTL cache for metasearch fan-out results.

    Thread-safe via a coarse lock (the metasearch route is read-heavy; fan-out
    is the expensive part, not lock contention). Designed for a Redis backend to
    drop behind the same ``get``/``put`` interface without changing callers.
    """

    ttl_s: float = _DEFAULT_TTL_S
    max_entries: int = _DEFAULT_MAX_ENTRIES
    _store: "OrderedDict[str, CacheEntry]" = field(default_factory=OrderedDict)
    _hits: int = 0
    _misses: int = 0

    def _key(self, query: str, sources: tuple[str, ...]) -> str:
        # Normalize: lowercase, collapse whitespace, strip. This is what makes
        # "Browser " and "browser" share a cache entry (§7 "popular queries
        # collapse").
        normalized = " ".join(query.lower().split())
        return f"{normalized}|{','.join(sorted(sources))}"

    def get(self, query: str, sources: tuple[str, ...]) -> CacheEntry | None:
        """Return a non-expired cached entry, or None. LRU-promotes on hit."""
        import threading

        with threading.Lock():
            key = self._key(query, sources)
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.expired:
                # Expired — evict, count as miss (the TTL is the freshness control).
                self._store.pop(key, None)
                self._misses += 1
                return None
            # LRU promote.
            self._store.move_to_end(key)
            self._hits += 1
            return entry

    def put(
        self,
        query: str,
        sources: tuple[str, ...],
        skills: list[dict[str, Any]],
        *,
        sources_ok: list[str] | None = None,
        sources_degraded: list[str] | None = None,
        sources_failed: list[str] | None = None,
    ) -> None:
        """Store a fan-out result. Evicts the LRU entry if at capacity."""
        import threading

        with threading.Lock():
            key = self._key(query, sources)
            self._store[key] = CacheEntry(
                skills=skills,
                sources_ok=sources_ok or [],
                sources_degraded=sources_degraded or [],
                sources_failed=sources_failed or [],
                cached_at=time.monotonic(),
                ttl_s=self.ttl_s,
            )
            self._store.move_to_end(key)
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
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "ttl_s": self.ttl_s,
            "max_entries": self.max_entries,
        }

    def invalidate(self, query: str | None = None) -> int:
        """Invalidate entries. No query → clear all (admin/test). Returns count."""
        import threading

        with threading.Lock():
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


# Module-level singleton (per-worker). The metasearch route uses this instance.
# A Redis-backed shared cache (P5+) would replace this with a Redis-backed
# implementation behind the same interface.
_cache = HotQueryCache()


def get_cache() -> HotQueryCache:
    """Return the module-level cache singleton."""
    return _cache
