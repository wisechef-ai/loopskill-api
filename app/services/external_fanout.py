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
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from app.services.federation import ExternalSkill, SourceAdapter

logger = logging.getLogger(__name__)

_MAX_WORKERS = 8


@dataclass
class ExternalFanoutResult:
    """One source's contribution to a live external-route fan-out."""

    source_id: str
    skills: "list[ExternalSkill]" = field(default_factory=list)
    ok: bool = False
    elapsed_s: float = 0.0
    reason: str = ""


def _cache_key(source_id: str, query: str, limit: int) -> str:
    q_norm = " ".join((query or "").strip().lower().split())
    return f"extroute:{source_id}:{q_norm}:{limit}"


def _query_one(
    source_id: str, adapter: "SourceAdapter | None", query: str, limit: int
) -> ExternalFanoutResult:
    """Query one adapter, cached by ``(source_id, normalized query, limit)``.

    Reuses ``federation_live``'s existing module-level TTL cache (``fl._cache``)
    and its ``_SEARCH_TTL_S`` constant — see module docstring.
    """
    import app.services.federation_live as fl

    key = _cache_key(source_id, query, limit)
    cached = fl._cache.get(key, fl._SEARCH_TTL_S)
    if cached is not None:
        return ExternalFanoutResult(source_id, list(cached), ok=True, elapsed_s=0.0, reason="cache_hit")

    t0 = time.monotonic()
    try:
        found = adapter.search(query or "", limit=limit) if adapter else []
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
    fl._cache.put(key, found)
    return ExternalFanoutResult(source_id, found, ok=True, elapsed_s=elapsed, reason="ok")


def run_external_fanout(
    pending: "list[tuple[str, SourceAdapter | None]]",
    *,
    query: str,
    limit: int,
    per_source_deadline_s: float,
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
    """
    results: dict[str, ExternalFanoutResult] = {}
    if not pending:
        return results

    overall_deadline_s = per_source_deadline_s + 0.25  # parallel; small scheduling slack
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS)
    try:
        futures = {pool.submit(_query_one, src, adapter, query, limit): src for src, adapter in pending}
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
                logger.warning(
                    "external fan-out source '%s' exceeded deadline %.1fs", src, overall_deadline_s
                )
                results[src] = ExternalFanoutResult(src, [], ok=False, reason="timeout")
                fut.cancel()
    finally:
        # Do NOT block on hung upstream threads. Already-running fetches are
        # bounded by federation_live._HTTP_TIMEOUT_S and simply discard their
        # result when they eventually finish.
        pool.shutdown(wait=False, cancel_futures=True)

    return results
