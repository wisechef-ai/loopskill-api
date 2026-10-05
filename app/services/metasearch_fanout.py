"""Concurrent bounded fan-out orchestrator for metasearch (metasearch_0710 P0 —
council condition 1).

Replaces the sequential source loop in ``skill_routes.get_external_skills``
(``app/skill_routes.py:533-637`` — one source at a time, each up to a 12s
timeout) with a **concurrent, deadline-bounded, rate-limited** fan-out. Every
source is queried in parallel under:

  - a per-source **token bucket + circuit breaker** (``metasearch_ratelimit``) —
    a dry bucket or open breaker drops that source from THIS request (graceful
    degrade; the list still returns from healthy sources);
  - a hard **per-source deadline** enforced by the gather loop (1.2s for
    cached catalog sources, a measured longer budget for the three live-search
    sources — see ``_SOURCE_DEADLINE_S``) — a slow source cannot drag the
    unified latency past its own budget (council C6: the old 12s timeout made
    the <1.5s SLO impossible);
  - a **bounded fan-out** (top-N per source) so no single source floods the merge.

It also carries each source's **raw row dict** alongside the mapped
``ExternalSkill`` so ``metasearch.unify_external`` can recover the popularity
signal the adapters discard (council C5).

ClawHub query routing (re-verified 2026-10-04): ``/api/v1/skills`` ignores every
query parameter; only ``/api/v1/search?q=`` ranks by query. The routing lives in
``clawhub_search`` and every ClawHub caller goes through it.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass

from app.services import metasearch_ratelimit as rl
from app.services.federation import ExternalSkill
from app.services.federation_adapters import get_adapter
from app.services.github_taps import METASEARCH_TAP_SOURCES

logger = logging.getLogger(__name__)

# The v1 fan-out source set. ClawHub is INCLUDED (searchable — Adam condition 2b
# makes it non-*deployable*, not non-searchable). Ordering is irrelevant here;
# the merge ranks. github-oss stays dark without a token (graceful empty).
_BASE_FANOUT_SOURCES: tuple[str, ...] = (
    "skills-sh",
    "clawhub",
    "hermes-hub",
    "well-known",
    "browse-sh",
    "lobehub",
    "github-oss",
)

# First-class taps (in_metasearch=True) join the fan-out so their skills rank
# alongside owned skills. Derived from the tap-list, so flipping the flag in
# github_taps is the only edit needed. Deduped + order-stable (base first) to
# keep merge ranking deterministic.
DEFAULT_FANOUT_SOURCES: tuple[str, ...] = _BASE_FANOUT_SOURCES + tuple(
    s for s in METASEARCH_TAP_SOURCES if s not in _BASE_FANOUT_SOURCES
)

# §7.5 latency (2026-07-11): tightened 2.5s → 1.2s. With SWR serving the
# expiry-boundary tail, the only requests that pay a live fan-out are TRUE cold
# misses (first-ever query, or a hard-expired key past the grace window). Those
# must fit the §5.5 render budget (1500ms). A slow source now degrades out at
# 1.2s (parallel, so 1.2s IS the whole-gather wall-clock) instead of dragging
# the unified latency to ~2s. Healthy sources return in 100–400ms and are
# unaffected; the circuit breaker demotes a persistently-slow source so it stops
# being tried at all. The `sources_degraded` list stays honest about who missed.
_PER_SOURCE_DEADLINE_S = 1.2
# Scheduling slack added to every source's deadline (thread start + pool overhead).
_DEADLINE_SLACK_S = 0.25

# t_b9887867 (2026-10-05): the 1.2 s budget above suits catalog sources, which
# answer from an in-process cache. It never fit the three sources that make a
# LIVE upstream call per query. Measured FROM wisechef-hq in the app venv (200s,
# full rows, no rate limit or WAF):
#   clawhub     /api/v1/search?q=   1.62-2.14 s
#   github-oss  GitHub code search  0.53-1.76 s
#   skills-sh   /api/search         0.58-0.76 s, over budget under 14-thread load
# Under the shared 1.45 s gather they were in `sources_degraded` on nearly every
# cold compute, and each miss fed their breakers. They get their own budget. The
# gather now ends when every source has answered or passed ITS deadline, so a
# hung catalog source is still cut at 1.45 s and a fast query does not wait 3 s.
_SOURCE_DEADLINE_S: dict[str, float] = {
    "clawhub": 3.0,
    "github-oss": 3.0,
    "skills-sh": 2.5,
}
_PER_SOURCE_TOP_N = 25
# One thread per source, capped. The sources are I/O-bound HTTP calls, so a
# thread each is cheap; with the old fixed 8 for 14 sources, the last 6 could
# only START after earlier ones finished and routinely blew the shared overall
# deadline before their first byte (fed1004 — measured on prod 2026-10-04).
_MAX_WORKERS = 16


@dataclass
class SourceResult:
    """One source's contribution: mapped skills paired with their raw rows (so
    the unifier can recover popularity), plus a health flag."""

    source: str
    skills: list[ExternalSkill]
    raw_rows: list[dict]
    ok: bool
    reason: str = ""


def _clawhub_fetch_fixed(query: str) -> list[dict]:
    """ClawHub fetch for the fan-out. Kept as a named seam (tests and the
    ``_fetch_for`` table address it), but the query routing now lives in ONE
    place — ``federation_live.clawhub_fetch`` → ``clawhub_search.fetch_rows`` —
    so the fan-out, resolve and browse paths can never disagree on which
    ClawHub route honours a query again."""
    from app.services import federation_live as fl

    return fl.clawhub_fetch(query)


def _fetch_for(source: str):
    """Resolve the fetch callable for a source, applying the ClawHub fix."""
    from app.services.federation_live import LIVE_FETCH

    if source == "clawhub":
        return _clawhub_fetch_fixed
    return LIVE_FETCH.get(source)


def _query_one_source(source: str, query: str, *, limit: int) -> SourceResult:
    """Query ONE source under the rate-limit gate. Returns a SourceResult with
    ok=False (and empty skills) when the source is gated, errors, or is empty.

    Raw rows are captured by wrapping the fetch callable so the adapter's
    ``.search()`` still maps them AND we keep the originals for popularity.

    Council R2 (new MUST): this worker does NOT record breaker health itself. If
    it times out, the request-owning gather loop already classified the source as
    degraded; a late success/failure recorded from THIS still-running thread would
    corrupt that shared state after the response. Health recording is owned solely
    by ``fan_out`` (the request thread), keyed off the SourceResult it actually
    consumes. A straggler's result is simply discarded.
    """
    if not rl.acquire(source):
        return SourceResult(source, [], [], ok=False, reason="rate_limited_or_open_circuit")

    fetch = _fetch_for(source)
    if fetch is None:
        return SourceResult(source, [], [], ok=False, reason="no_fetch_callable")

    captured: list[dict] = []

    def _capturing_fetch(q: str) -> list[dict]:
        rows = fetch(q) or []
        captured.extend(rows)
        return rows

    adapter = get_adapter(source, fetch=_capturing_fetch)
    if adapter is None:
        return SourceResult(source, [], [], ok=False, reason="no_adapter")

    try:
        skills = adapter.search(query, limit=limit)
        return SourceResult(source, list(skills), list(captured), ok=True)
    # Rationale: a single source's failure must never break the fan-out — it is
    # dropped from this request; the OWNING thread records the breaker failure.
    except Exception:  # noqa: BLE001
        logger.warning("metasearch source '%s' search failed", source, exc_info=True)
        return SourceResult(source, [], [], ok=False, reason="fetch_error")


@dataclass
class FanoutOutput:
    """The fan-out's raw product: per-source (skill, raw_row) pairs + health."""

    pairs: list[tuple[ExternalSkill, dict]]
    sources_ok: list[str]
    sources_degraded: list[str]


def deadline_for(source: str, override: float | None = None) -> float:
    """The deadline (excluding slack) one source gets. An explicit ``override``
    applies uniformly to every source; otherwise live-search sources use their
    measured budget from ``_SOURCE_DEADLINE_S`` and the rest the default."""
    if override is not None:
        return override
    return _SOURCE_DEADLINE_S.get(source, _PER_SOURCE_DEADLINE_S)


def fan_out(
    query: str,
    *,
    sources: tuple[str, ...] = DEFAULT_FANOUT_SOURCES,
    per_source_top_n: int = _PER_SOURCE_TOP_N,
    per_source_deadline_s: float | None = None,
) -> FanoutOutput:
    """Query all sources CONCURRENTLY under per-source deadline + rate limit.

    Returns (ExternalSkill, raw_row) pairs so the caller can ``unify_external``
    with popularity, plus the ok/degraded source lists for the §8 predicate and
    the honest per-query "N results across M sources".

    ``per_source_deadline_s=None`` (the default) gives each source its own
    budget via ``deadline_for``; a number applies to every source.
    """
    pairs: list[tuple[ExternalSkill, dict]] = []
    ok: list[str] = []
    degraded: list[str] = []

    # Concurrent gather with a HARD wall-clock budget PER SOURCE. Council finding
    # 1 + R2: a hung source must never escape as an unhandled TimeoutError or
    # hold the request on shutdown(wait=True). Each source has its own cutoff
    # (start + deadline_for(src) + slack); the gather waits for the next event
    # (a completion or the earliest pending cutoff) and ends once every source
    # has answered or passed its cutoff. So a hung 1.2 s catalog source is cut
    # at 1.45 s even while ClawHub's 3 s budget is still open, and a query whose
    # sources all answer fast never waits for the longest budget (t_b9887867).
    # On a cutoff: mark the source degraded, record its breaker failure from
    # THIS (owning) thread, cancel it. The worker thread does NOT record health
    # (R2 race fix) — only this loop does. A straggler thread keeps running up
    # to _HTTP_TIMEOUT_S but its result is discarded and cannot mutate state.
    started = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(sources), _MAX_WORKERS)))
    try:
        futures = {pool.submit(_query_one_source, src, query, limit=per_source_top_n): src for src in sources}
        cutoff = {
            fut: started + deadline_for(src, per_source_deadline_s) + _DEADLINE_SLACK_S
            for fut, src in futures.items()
        }
        pending = set(futures)
        while pending:
            now = time.monotonic()
            for fut in [f for f in pending if cutoff[f] <= now]:
                pending.discard(fut)
                src = futures[fut]
                if fut.done():  # finished at the wire: its answer is already here
                    _consume(src, fut, pairs, ok, degraded)
                    continue
                logger.warning(
                    "metasearch source '%s' exceeded its deadline %.2fs", src, cutoff[fut] - started
                )
                rl.record_failure(src)
                if src not in degraded:
                    degraded.append(src)
                fut.cancel()
            if not pending:
                break
            done, _ = concurrent.futures.wait(
                pending,
                timeout=max(0.0, min(cutoff[f] for f in pending) - now),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done:
                pending.discard(fut)
                _consume(futures[fut], fut, pairs, ok, degraded)
    finally:
        # Do NOT block on hung upstream threads (cancel_futures drops queued work;
        # already-running fetches are bounded by _HTTP_TIMEOUT_S). Python 3.9+.
        pool.shutdown(wait=False, cancel_futures=True)

    return FanoutOutput(pairs=pairs, sources_ok=ok, sources_degraded=degraded)


def _consume(
    src: str,
    fut: concurrent.futures.Future,
    pairs: list[tuple[ExternalSkill, dict]],
    ok: list[str],
    degraded: list[str],
) -> None:
    """Fold one finished source into the gather and record its breaker outcome
    (from the owning request thread, never the worker — council R2)."""
    try:
        result = fut.result()
    # Rationale: a worker raising must not abort the fan-out gather.
    except Exception:  # noqa: BLE001
        logger.warning("metasearch source '%s' worker error", src, exc_info=True)
        rl.record_failure(src)
        degraded.append(src)
        return
    if not result.ok:
        # A GATED source (open circuit / dry bucket) never leased a probe, so it
        # needs no outcome. Everything else consumed one and must resolve it, or
        # the breaker holds a lease for a call that never happened (mesh0408e2e).
        if result.reason != "rate_limited_or_open_circuit":
            rl.record_failure(src)
        degraded.append(src)
        return
    rl.record_success(src)
    ok.append(src)
    rows = result.raw_rows
    for i, skill in enumerate(result.skills):
        pairs.append((skill, rows[i] if i < len(rows) else {}))
