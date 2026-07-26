"""Concurrent, deadline-bounded fan-out for the legacy ``/api/skills/external``
route's LIVE (non-empty-query) search path — spotify2607fix_2 (HIGH-severity
perf fix, tori sp2607fix-2).

THE BUG (measured live against https://app.loopskill.io, 2026-07-26):
``skill_routes.get_external_skills`` walked ``LIVE_SOURCES`` SERIALLY for any
non-empty query, calling ``adapter.search()`` one source at a time with NO
per-source timeout of its own — each underlying HTTP call is only bounded by
``federation_live._HTTP_TIMEOUT_S`` (12s), and ``guarded_get`` re-times EVERY
redirect hop (5-hop cap), so one slow/rate-limited/redirect-heavy upstream
could singlehandedly hold the whole request for tens of seconds, and seven
sources queued one after another could serialize into 131s+ (``q=docker``) or
never return at all (``q=humanizer``, >180s). The empty-query path was
unaffected — it serves the persistent per-source cache
(``federation_cache.read_first_page``) and never reaches this fan-out.

THE FIX: every source that still needs a LIVE fetch (non-empty query, admin
refresh, or first-boot-before-cron) is queried CONCURRENTLY under a hard
per-source deadline (``app.config.settings.EXTERNAL_FANOUT_PER_SOURCE_DEADLINE_S``,
default 2.5s). Because sources run in parallel, the deadline (plus a small
scheduling slack) IS the whole-gather wall-clock — a hung source cannot drag
the total past ~2.5s regardless of how many OTHER sources are also queried.
A source that doesn't finish in time is abandoned (its thread is left running
to completion in the background; the result is simply discarded — never
awaited) and reported as degraded; every source that DID finish in time still
contributes its results. This mirrors the identical, already-proven pattern in
``app/services/metasearch_fanout.fan_out`` (straggler-safe cancel, no
``fut.result()`` on a stale future, ``pool.shutdown(wait=False,
cancel_futures=True)`` so a hung upstream thread never blocks process
shutdown or the request).

CACHING: a repeated non-empty query within the TTL window is served without
re-touching the network at all. This reuses ``federation_live``'s EXISTING
in-process TTL cache (the module-level ``_TTLCache`` instance ``fl._cache``
and its ``_SEARCH_TTL_S`` constant — the same mechanism already backing
``skills_sh_fetch`` / ``clawhub_fetch``'s own per-query caching) rather than
introducing a second, parallel caching layer. The key is extended one layer up
to ``(source_id, normalized_query, limit)`` so the ADAPTER-mapped result
(post ``ExternalSkill`` mapping + limit slice) is cached, not just the raw
fetch row — a cache hit here skips the adapter mapping too, not only the HTTP
call.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.services.federation import ExternalSkill, SourceAdapter

logger = logging.getLogger(__name__)

_MAX_WORKERS = 8

# Codex review MUST-FIX 2: ONE process-wide bounded pool, not one per request.
# A per-request executor let abandoned straggler threads accumulate without
# limit under sustained slow-upstream load (cancel_futures only cancels work
# that never started, and 7 sources against 8 workers all start immediately).
# A shared pool caps total in-flight upstream work at _MAX_WORKERS for the
# whole process. Lazily created + double-checked under a lock so import order
# and test monkeypatching stay simple; never shut down (process-lifetime).
_pool_lock = threading.Lock()
_pool: "concurrent.futures.ThreadPoolExecutor | None" = None


def _shared_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=_MAX_WORKERS, thread_name_prefix="extfanout"
                )
    return _pool


@dataclass
class ExternalFanoutResult:
    """One source's contribution to a live external-route fan-out."""

    source_id: str
    skills: "list[ExternalSkill]" = field(default_factory=list)
    ok: bool = False
    elapsed_s: float = 0.0
    reason: str = ""


def _cache_key(source_id: str, query: str, limit: int) -> str:
    # Codex review SHOULD-FIX (accepted): the key normalises the query but the
    # ADAPTER receives the raw string, so "  Docker  " and "docker" would share
    # a cache entry while potentially producing different upstream results.
    # Resolved by normalising ONCE, up front, and passing the SAME normalised
    # string to both the key and adapter.search() (see _query_one) — key and
    # execution semantics now agree by construction.
    return f"extroute:{source_id}:{normalize_query(query)}:{limit}"


def normalize_query(query: str) -> str:
    """Canonical query form used for BOTH the cache key and the adapter call."""
    return " ".join((query or "").strip().lower().split())


def _query_one(
    source_id: str,
    adapter: "SourceAdapter | None",
    query: str,
    limit: int,
    *,
    force_refresh: bool = False,
    deadline_at: float | None = None,
) -> ExternalFanoutResult:
    """Query one adapter, cached by ``(source_id, normalized query, limit)``.

    Reuses ``federation_live``'s existing module-level TTL cache (``fl._cache``)
    and its ``_SEARCH_TTL_S`` constant — see module docstring.

    ``force_refresh`` (Codex review MUST-FIX 3) BYPASSES the query cache. An
    admin ``?refresh=1`` must actually re-touch the upstream: without this the
    route could serve a TTL-cached result and then write it into the PERSISTENT
    source cache as if freshly walked, quietly defeating the one mechanism an
    operator has to force a real refresh.

    ``deadline_at`` (Codex review MUST-FIX 1) is a ``time.monotonic()`` instant
    after which this worker's result is STALE — the request that spawned it has
    already returned without it. A late worker must NOT write to the shared
    cache: its result could overwrite a NEWER cached value for the same key
    (last-writer-wins across overlapping requests), so a slow upstream would
    poison a fast one. Past the deadline we return the rows to the (already
    gone) caller but skip the ``put`` entirely.
    """
    import app.services.federation_live as fl

    q_norm = normalize_query(query)
    key = _cache_key(source_id, query, limit)
    if not force_refresh:
        cached = fl._cache.get(key, fl._SEARCH_TTL_S)
        if cached is not None:
            return ExternalFanoutResult(source_id, list(cached), ok=True, elapsed_s=0.0, reason="cache_hit")

    t0 = time.monotonic()
    try:
        found = adapter.search(q_norm, limit=limit) if adapter else []
    # Rationale: one source's failure must never break the fan-out for the
    # others — it is reported as degraded here; the request-owning thread
    # (run_external_fanout) is the only one that consumes this result, so no
    # shared/global state is mutated from this worker thread.
    except Exception:  # noqa: BLE001
        elapsed = time.monotonic() - t0
        logger.warning(
            "external fan-out source '%s' search failed after %.2fs", source_id, elapsed, exc_info=True
        )
        return ExternalFanoutResult(source_id, [], ok=False, elapsed_s=elapsed, reason="fetch_error")
    elapsed = time.monotonic() - t0
    # Only a result that beat the deadline may enter the shared cache.
    if deadline_at is None or time.monotonic() <= deadline_at:
        fl._cache.put(key, found)
    else:
        logger.debug(
            "external fan-out source '%s' finished %.2fs late — result NOT cached", source_id, elapsed
        )
    return ExternalFanoutResult(source_id, found, ok=True, elapsed_s=elapsed, reason="ok")


def run_external_fanout(
    pending: "list[tuple[str, SourceAdapter | None]]",
    *,
    query: str,
    limit: int,
    per_source_deadline_s: float,
    force_refresh: bool = False,
) -> dict[str, ExternalFanoutResult]:
    """Query every ``(source_id, adapter)`` pair in ``pending`` CONCURRENTLY,
    bounded by a hard per-source deadline. Returns ``{source_id: ExternalFanoutResult}``.

    Because every source runs in parallel, the per-source deadline IS (plus a
    small scheduling slack) the whole-gather wall-clock — a slow/hung source
    cannot drag the response past ``~per_source_deadline_s`` regardless of how
    many OTHER sources are queried. A source that doesn't finish in time is
    marked ``ok=False, reason="timeout"`` (degraded) and its thread is left to
    run to completion in the background (result discarded) rather than
    blocking the request — identical pattern to ``metasearch_fanout.fan_out``.

    Codex review MUST-FIX 2 (CONFIRMED): this used to create a NEW
    ``ThreadPoolExecutor`` per request. ``cancel_futures=True`` only cancels
    work that has not STARTED, and with 7 sources against 8 workers all seven
    start immediately — so every timed-out request abandoned up to seven live
    threads, each holding a socket for up to ``_HTTP_TIMEOUT_S`` (12s) per
    redirect hop. The RESPONSE was bounded; RESOURCE USE was not, and under
    sustained slow-upstream load threads accumulate without limit.

    Fixed by sharing ONE process-wide bounded pool (``_shared_pool``). The
    response stays bounded by the deadline exactly as before, but total
    in-flight upstream work is now capped at ``_MAX_WORKERS`` across the whole
    process instead of growing linearly with concurrent requests. When the pool
    is saturated a queued source is reported ``reason="saturated"`` — honest
    degradation, and a signal worth alerting on rather than a silent stall.
    """
    results: dict[str, ExternalFanoutResult] = {}
    if not pending:
        return results

    overall_deadline_s = per_source_deadline_s + 0.25  # parallel; small scheduling slack
    deadline_at = time.monotonic() + overall_deadline_s
    pool = _shared_pool()
    futures = {
        pool.submit(
            _query_one,
            src,
            adapter,
            query,
            limit,
            force_refresh=force_refresh,
            deadline_at=deadline_at,
        ): src
        for src, adapter in pending
    }
    still_pending = set(futures)
    try:
        for fut in concurrent.futures.as_completed(futures, timeout=overall_deadline_s):
            still_pending.discard(fut)
            src = futures[fut]
            try:
                results[src] = fut.result()
            # Rationale: a worker future raising unexpectedly (vs. a
            # handled fetch_error inside _query_one) must not abort the
            # whole gather — treat it exactly like a normal degradation.
            except Exception:  # noqa: BLE001
                logger.warning("external fan-out source '%s' worker error", src, exc_info=True)
                results[src] = ExternalFanoutResult(src, [], ok=False, reason="worker_error")
    except concurrent.futures.TimeoutError:
        # Deadline hit: every still-pending source is degraded, not fatal
        # — partial results from the sources that DID finish still stand.
        for fut in still_pending:
            src = futures[fut]
            # Distinguish "the upstream was slow" from "we never got a worker".
            # cancel() succeeds only for work that never started, which on a
            # SHARED pool means it was queued behind other requests.
            never_started = fut.cancel()
            reason = "saturated" if never_started else "timeout"
            logger.warning(
                "external fan-out source '%s' degraded (%s) at deadline %.1fs",
                src,
                reason,
                overall_deadline_s,
            )
            results[src] = ExternalFanoutResult(src, [], ok=False, reason=reason)

    # NOTE: the shared pool is deliberately NOT shut down here — it is
    # process-wide and outlives the request. Straggler threads finish on their
    # own (bounded by federation_live._HTTP_TIMEOUT_S) and their results are
    # discarded; `deadline_at` stops them writing a stale value to the cache.
    return results
