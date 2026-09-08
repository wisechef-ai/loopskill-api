"""Single-flight + stale-while-revalidate orchestration for the metasearch cache.

Split out of ``metasearch_cache.py`` (unisearch_0709 P1) to keep that module
under the repo's 600-line god-object cap once the Redis L2 landed. The seam is a
real one: ``metasearch_cache`` owns STORAGE and the freshness state machine
across the two tiers; this mixin owns the CONCURRENCY policy layered on top —
who computes, who waits, and who refreshes in the background.

That policy is deliberately per-PROCESS (unisearch_0709 §12(f)): the in-flight
Event and the refresh guard are process-local, and there is NO cross-process
lock. Worst case, N workers each compute a cold query once. That is accepted and
intentional — a distributed lock would buy one saved fan-out at the cost of an
availability dependency on Redis for a path whose whole point is that it keeps
working when Redis does not.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class SingleFlightSWRMixin:
    """``get_or_compute`` and its background-refresh machinery.

    Mixed into ``HotQueryCache``; it uses that class's lock, store and the
    ``get``/``put``/``put_if_current`` interface, and adds no state of its own.
    """

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
        # Judged on the ENTRY, not the lookup state, so a ``degraded`` shared tier
        # still serves the L1 payload instead of stampeding upstream — a Redis
        # outage must not turn into an upstream outage.
        entry, _state = self.get_entry(query, sources)
        if entry is not None and entry.fresh:
            return entry, False
        if entry is not None and entry.stale:
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
