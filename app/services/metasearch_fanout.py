"""Concurrent bounded fan-out orchestrator for metasearch (metasearch_0710 P0 —
council condition 1).

Replaces the sequential source loop in ``skill_routes.get_external_skills``
(``app/skill_routes.py:533-637`` — one source at a time, each up to a 12s
timeout) with a **concurrent, deadline-bounded, rate-limited** fan-out. Every
source is queried in parallel under:

  - a per-source **token bucket + circuit breaker** (``metasearch_ratelimit``) —
    a dry bucket or open breaker drops that source from THIS request (graceful
    degrade; the list still returns from healthy sources);
  - a hard **per-source deadline** (default 2.5s) enforced by the thread pool —
    a slow source cannot drag the unified p95 (council C6: the 12s timeout made
    the <1.5s SLO impossible);
  - a **bounded fan-out** (top-N per source) so no single source floods the merge.

It also carries each source's **raw row dict** alongside the mapped
``ExternalSkill`` so ``metasearch.unify_external`` can recover the popularity
signal the adapters discard (council C5).

ClawHub query-param bug (council C1 + live probe 2026-07-10): the existing
``clawhub_fetch`` sends ``?q=`` but ClawHub's ``/api/v1/skills`` wants
``?search=``. Fixed at the fetch layer in this module's ``_clawhub_fetch_fixed``
wrapper so the fan-out gets real ClawHub results.
"""

from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass

from app.services import metasearch_ratelimit as rl
from app.services.federation import ExternalSkill
from app.services.federation_adapters import get_adapter

logger = logging.getLogger(__name__)

# The v1 fan-out source set. ClawHub is INCLUDED (searchable — Adam condition 2b
# makes it non-*deployable*, not non-searchable). Ordering is irrelevant here;
# the merge ranks. github-oss stays dark without a token (graceful empty).
DEFAULT_FANOUT_SOURCES: tuple[str, ...] = (
    "skills-sh",
    "clawhub",
    "hermes-hub",
    "well-known",
    "browse-sh",
    "lobehub",
    "github-oss",
)

_PER_SOURCE_DEADLINE_S = 2.5
_PER_SOURCE_TOP_N = 25
_MAX_WORKERS = 8


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
    """ClawHub fetch with the correct ``?search=`` param (council C1 fix).

    The shipped ``federation_live.clawhub_fetch`` sends ``?q=`` which ClawHub's
    ``/api/v1/skills`` ignores (verified 2026-07-10: ``?q=`` → default page,
    ``?search=`` → real matches). We call the live JSON getter directly with the
    right param and the same short TTL cache key namespace.
    """
    from app.services import federation_live as fl

    q = (query or "").strip()
    cache_key = f"clawhub-fixed:{q.lower()}"
    cached = fl._cache.get(cache_key, fl._SEARCH_TTL_S)
    if cached is not None:
        return cached
    params: dict[str, object] = {"limit": 100}
    if q:
        params["search"] = q  # the fix: ?search=, not ?q=
    data = fl._safe_json_get(fl.CLAWHUB_SKILLS_URL, params=params)
    rows = data.get("items", []) if isinstance(data, dict) else []
    rows = rows if isinstance(rows, list) else []
    fl._cache.put(cache_key, rows)
    return rows


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
        rl.record_success(source)
        return SourceResult(source, list(skills), list(captured), ok=True)
    # Rationale: a single source's failure must never break the fan-out — it is
    # dropped from this request and the breaker counts it toward opening.
    except Exception:  # noqa: BLE001
        logger.warning("metasearch source '%s' search failed", source, exc_info=True)
        rl.record_failure(source)
        return SourceResult(source, [], [], ok=False, reason="fetch_error")


@dataclass
class FanoutOutput:
    """The fan-out's raw product: per-source (skill, raw_row) pairs + health."""

    pairs: list[tuple[ExternalSkill, dict]]
    sources_ok: list[str]
    sources_degraded: list[str]


def fan_out(
    query: str,
    *,
    sources: tuple[str, ...] = DEFAULT_FANOUT_SOURCES,
    per_source_top_n: int = _PER_SOURCE_TOP_N,
    per_source_deadline_s: float = _PER_SOURCE_DEADLINE_S,
) -> FanoutOutput:
    """Query all sources CONCURRENTLY under per-source deadline + rate limit.

    Returns (ExternalSkill, raw_row) pairs so the caller can ``unify_external``
    with popularity, plus the ok/degraded source lists for the §8 predicate and
    the honest per-query "N results across M sources".
    """
    pairs: list[tuple[ExternalSkill, dict]] = []
    ok: list[str] = []
    degraded: list[str] = []

    # Concurrent gather with a HARD wall-clock budget. Council finding 1: the
    # prior `as_completed(timeout=deadline*len)` was a whole-gather timeout and
    # `fut.result()` on an already-completed future could never fire, so a hung
    # source escaped as an unhandled TimeoutError AND the `with` block blocked on
    # shutdown(wait=True). Fix: one overall deadline, catch the iterator timeout,
    # mark every not-yet-done source degraded, and cancel (don't wait on) the
    # stragglers. The real upstream HTTP timeout (_HTTP_TIMEOUT_S) is the backstop
    # if a worker thread can't be cancelled mid-flight; the request still returns.
    overall_deadline_s = per_source_deadline_s + 1.0  # sources run in parallel, not serial
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS)
    try:
        futures = {pool.submit(_query_one_source, src, query, limit=per_source_top_n): src for src in sources}
        pending = set(futures)
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=overall_deadline_s):
                pending.discard(fut)
                src = futures[fut]
                try:
                    result = fut.result()
                # Rationale: a worker raising must not abort the fan-out gather.
                except Exception:  # noqa: BLE001
                    logger.warning("metasearch source '%s' worker error", src, exc_info=True)
                    degraded.append(src)
                    continue
                if not result.ok:
                    degraded.append(src)
                    continue
                ok.append(src)
                rows = result.raw_rows
                for i, skill in enumerate(result.skills):
                    raw = rows[i] if i < len(rows) else {}
                    pairs.append((skill, raw))
        except concurrent.futures.TimeoutError:
            # Deadline hit: every still-pending source is degraded, not fatal.
            for fut in pending:
                src = futures[fut]
                logger.warning(
                    "metasearch source '%s' exceeded overall deadline %.1fs", src, overall_deadline_s
                )
                rl.record_failure(src)
                if src not in degraded:
                    degraded.append(src)
                fut.cancel()
    finally:
        # Do NOT block on hung upstream threads (cancel_futures drops queued work;
        # already-running fetches are bounded by _HTTP_TIMEOUT_S). Python 3.9+.
        pool.shutdown(wait=False, cancel_futures=True)

    return FanoutOutput(pairs=pairs, sources_ok=ok, sources_degraded=degraded)
