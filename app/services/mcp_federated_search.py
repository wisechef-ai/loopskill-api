"""Federated append for the MCP search path: cache first, then a bounded live
fan-out, then the local hub index (unisearch_0709 P2, reworked in fed1004).

The problem, verified live 2026-09-08: ``loopskill_search("graft")`` returned
``{"results": [], "total": 0}`` while the metasearch fan-out found
``skills-sh:trailhq--graft--graft`` and ``loopskill_install`` installed it fine.
P2 appended rows from the shared cache, but ONLY from the cache — so on
2026-10-04 every query no REST caller had warmed still answered ``cold`` with
zero rows (``loopskill_search("ste100")``: nothing; REST metasearch: 30 rows).

Three rules this module exists to hold, none of which the MCP tool restates:

1. **Bounded wall-clock, bounded work, ONE key.** Cache reads take
   microseconds. On a miss, ONE live fan-out starts on a warm slot (the same
   compute, cache key and per-source deadline as ``GET /api/skills/metasearch``,
   so the two surfaces single-flight together) and a local-index query starts
   in parallel on a floor slot; the caller waits at most
   ``settings.MCP_FEDERATED_LIVE_BUDGET_S`` in total. A slot is held for the
   whole real compute (stale refreshes run synchronously via ``refresh_now``),
   so ``metasearch_cache_swr.MAX_BACKGROUND_REFRESHES (shared with REST refreshes)`` caps fan-outs, not launchers. Budget 0 restores the P2 cache-only rows and flags; hit/miss
   stats stay REST-only either way (reads use ``_count=False``).
2. **Honest freshness.** ``fresh`` / ``stale`` / ``degraded`` mirror the cache
   reader. ``warming``: a live fan-out is still running; rows come from the
   local index. ``cold``: no live result is available (budget 0, every slot
   busy, or the fan-out failed); rows, if any, come from the local index.
   Never an empty-but-``fresh`` answer, never an exception.
3. **The source's verdict outranks the cache's claim.** A cached row's
   ``deployable`` field is a claim written by whoever wrote the entry. The
   deployability returned here is RE-DERIVED from the row's own descriptor
   through the existing install router (``federation.route_install``) and the
   existing fleet-deploy allow-list, then AND-ed with that claim — so a
   poisoned entry can only ever narrow the verdict, never widen it.

Context discipline: the appended rows are COMPACT
(``slug``/``title``/``install_ref``/``deployable``/``install_path``/
``origin_url``/``quality``). Full bodies stay behind ``loopskill_install`` —
the agent gets what it needs to decide and act, not a catalog dump in its
context window.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Live fan-outs (cold computes AND stale refreshes) that may run at once in this
# worker. Each fan-out runs one thread per source, so this caps the source
# threads at metasearch_cache_swr.MAX_BACKGROUND_REFRESHES (shared with REST refreshes) x len(sources). Past it, a query is answered
# from the local index and flagged ``cold``.
# fed1007 R2: ONE process-wide budget for every background fan-out. MCP warms
# and REST stale-while-revalidate refreshes take slots from the same semaphore,
# so a mixed REST+MCP burst after a deploy never exceeds it. A full budget makes
# MCP answer from the local index with the "cold" flag (designed fallback).
from app.services.metasearch_cache_swr import (  # noqa: E402
    _REFRESH_SLOTS as _warm_slots,
)

# Local-index queries that may run at once. A slow or locked table must not
# pile up one blocked thread per MCP call.
MAX_CONCURRENT_FLOORS = 4
_floor_slots = threading.BoundedSemaphore(MAX_CONCURRENT_FLOORS)

# Hard ceiling on the budget whatever the setting says: an agent waiting longer
# than this on a search reads the platform as broken.
_MAX_LIVE_BUDGET_S = 10.0

# The append cap. 10 is the default an agent gets without asking; 30 is the
# ceiling an explicit caller can raise it to. Both are context-window budgets,
# not relevance judgements — the cached list is already ranked.
FEDERATED_DEFAULT_CAP = 10
FEDERATED_MAX_CAP = 30

# Per-field caps for the compact row. The shared cache already caps strings at
# 2 KB each (metasearch_cache_l2.MAX_STR_LEN); that is a poisoning guard, not a
# context-window guard — 30 rows x 4 x 2 KB is a quarter-megabyte of agent
# context. These are the context-window guard.
_MAX_SLUG_LEN = 200
_MAX_TITLE_LEN = 200
_MAX_REF_LEN = 220
_MAX_URL_LEN = 300
_MAX_SHORT_LEN = 40  # install_path / quality — closed vocabularies

# The documented ceiling on the whole append, so a caller (and a test) can state
# the blast radius on an agent's context in bytes rather than in adjectives.
FEDERATED_ROW_MAX_BYTES = 1_200
FEDERATED_APPEND_MAX_BYTES = FEDERATED_MAX_CAP * FEDERATED_ROW_MAX_BYTES

# Reader state → the honest wire flag. ``miss`` is reported as ``cold`` because
# that is what it means to the caller: this worker has nothing warm for this
# query, and we are NOT going to go get it on their thread.
_STATE_TO_FLAG = {"fresh": "fresh", "stale": "stale", "miss": "cold", "degraded": "degraded"}
WARMING = "warming"

# Curated rows live in the cached list too (the REST route merges them in). They
# are NOT federated and the MCP native pass already returns them from the DB, so
# they are dropped here rather than shipped twice in two different shapes.
_CURATED_SOURCE = "recipes"


def federated_sources() -> tuple[str, ...]:
    """The source tuple the cache key is built from.

    MUST match what the REST route passes to ``get_or_compute`` or the two
    surfaces compute different keys and the MCP read misses every entry the REST
    route just warmed. Imported from the fan-out module for that single source of
    truth — importing the constant is not calling ``fan_out``.
    """
    from app.services.metasearch_fanout import DEFAULT_FANOUT_SOURCES

    return tuple(DEFAULT_FANOUT_SOURCES)


def _text(value: Any, cap: int) -> str:
    return str(value or "")[:cap]


def _deployable_verdict(row: dict[str, Any]) -> bool:
    """Re-derive whether a cached row may claim ``deployable``.

    Reuses the EXISTING redistribution verdict — ``federation.route_install``
    plus ``metasearch.is_fleet_deployable_source`` — exactly as the fan-out did
    when the row was built. No new licence predicate is invented here; that is
    the point. ``route_install`` denies a DEEP_LINK row outright and denies a
    FETCH_ORIGIN row whose licence forbids redistribution.

    Where ``redistributable`` comes from: the cached card shape does not carry
    it, because the adapters encode that verdict IN the install path — a licence
    that forbids redistribution is emitted as ``deep_link``, never as
    ``fetch_origin`` (``federation_adapters``). That is the same equivalence
    ``bundle_wellknown_routes._is_redistributable_external`` relies on
    (``redistributable AND install_path == 'fetch_origin'``). So an explicit
    field is honoured when present, and the path is the fallback when it is not.

    The row's own ``deployable`` claim is AND-ed in last. A shared cache is a
    fleet-wide blast radius: an entry that claims ``deployable: true`` for a
    deep-link row must not be able to promote it. The conjunction can only ever
    narrow the answer, never widen it.
    """
    from app.services.federation import ExternalSkill, InstallPath, route_install
    from app.services.metasearch import is_fleet_deployable_source

    source = str(row.get("source") or "")
    try:
        path = InstallPath(str(row.get("install_path") or ""))
    except ValueError:
        return False  # unrecognised install path → fail closed, never deployable

    declared = row.get("redistributable")
    redistributable = bool(declared) if declared is not None else path is InstallPath.FETCH_ORIGIN

    probe = ExternalSkill(
        slug=str(row.get("slug") or ""),
        title=str(row.get("title") or ""),
        source=source,
        install_path=path,
        origin_url=str(row.get("origin_url") or ""),
        license=row.get("license"),
        redistributable=redistributable,
    )
    return route_install(probe).allowed and is_fleet_deployable_source(source) and bool(row.get("deployable"))


def compact_row(row: Any) -> dict[str, Any] | None:
    """Map one cached metasearch card to the compact federated row, or None.

    None means "not appendable": a non-dict, a curated row (the native pass owns
    those), or a row with no ``install_ref`` — without a ref the agent has
    nothing to hand ``loopskill_install``, and a result it cannot act on is
    noise in its context window.
    """
    if not isinstance(row, dict):
        return None
    if str(row.get("source") or "") == _CURATED_SOURCE:
        return None
    install_ref = _text(row.get("install_ref"), _MAX_REF_LEN)
    if not install_ref:
        return None
    compact = {
        "slug": _text(row.get("slug"), _MAX_SLUG_LEN),
        "title": _text(row.get("title") or row.get("slug"), _MAX_TITLE_LEN),
        "install_ref": install_ref,
        "deployable": _deployable_verdict(row),
        "install_path": _text(row.get("install_path"), _MAX_SHORT_LEN),
        "origin_url": _text(row.get("origin_url"), _MAX_URL_LEN),
        "quality": _text(row.get("quality"), _MAX_SHORT_LEN),
    }
    if row.get("relaxed") is True:
        # ah_1010: a near match (one query word missing) stays labelled as one.
        compact["relaxed"] = True
    return compact


def _warm_query(query: str, sources: tuple[str, ...]) -> None:
    """The live fan-out into the shared cache key. A module attribute so tests
    can swap it. Runs SYNCHRONOUSLY on the caller's (slot-holding) thread."""
    from app.services.metasearch_compute import warm

    warm(query, sources)


def live_budget_s() -> float:
    """The live-fan-out wait budget, clamped to ``[0, _MAX_LIVE_BUDGET_S]``."""
    from app.config import settings

    try:
        budget = float(getattr(settings, "MCP_FEDERATED_LIVE_BUDGET_S", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(budget, _MAX_LIVE_BUDGET_S))


class _Job:
    """One bounded background job: a done-Event plus its result or error."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


def _run_bounded(slots: threading.BoundedSemaphore, fn: Callable[[], Any], name: str) -> _Job | None:
    """Run ``fn`` on a daemon thread that holds one of ``slots`` until ``fn``
    returns. None when every slot is busy, or when the thread cannot start (the
    slot is released — a start failure must never leak capacity)."""
    if not slots.acquire(blocking=False):
        return None
    job = _Job()

    def _run() -> None:
        try:
            job.result = fn()
        # Rationale: a background job is best-effort; its failure is recorded
        # on the job and logged, never raised into anything.
        except BaseException as exc:  # noqa: BLE001
            job.error = exc
            logger.warning("%s failed", name, exc_info=True)
        finally:
            slots.release()
            job.done.set()

    try:
        threading.Thread(target=_run, name=name, daemon=True).start()
    except BaseException:  # noqa: BLE001 — RuntimeError("can't start new thread") and friends
        slots.release()
        logger.warning("%s could not start a thread; slot released", name, exc_info=True)
        return None
    return job


def local_floor(
    query: str | None, *, limit: int, exclude_slugs: frozenset[str] | set[str]
) -> list[dict[str, Any]]:
    """Compact rows from the LOCAL hub index — no network, one bounded DB query.

    ``federation_hub_skills`` holds the ingested Hermes Hub snapshot (~100k
    skills across skills.sh, ClawHub, GitHub, LobeHub, browse.sh and the
    official set). It is what an agent gets while a live fan-out is still
    running, so a first-time query is never empty when the index knows the
    answer. Empty query → no floor (a bare catalog listing is the native pass's
    job). Deployability is re-derived per row by ``compact_row`` (rule 3).
    """
    if not (query or "").strip():
        return []
    try:
        from app.services.hub_local_search import search_hub_index, search_hub_index_relaxed
        from app.services.metasearch import unify_external

        fetch = max(limit * 2, 10)
        hits = search_hub_index(query, limit=fetch)
        relaxed = False
        if not hits:
            # ah_1010: a 3+ subject-word query lets one word miss (see
            # metasearch_compute._relaxed_or); the rows are flagged relaxed.
            hits = search_hub_index_relaxed(query, limit=fetch)
            relaxed = bool(hits)
        rows: list[dict[str, Any]] = []
        for skill in hits:
            if len(rows) >= limit:
                break
            compact = compact_row(unify_external(skill).to_dict())
            if compact is None or compact["slug"] in exclude_slugs:
                continue
            if relaxed:
                compact["relaxed"] = True
            rows.append(compact)
        return rows
    # Rationale: the floor is a fallback for a fallback — a DB hiccup here
    # degrades to "no floor rows", never to a failed search.
    except Exception:  # noqa: BLE001
        logger.warning("federated local floor failed for %r", query, exc_info=True)
        return []


def _rows_from_entry(
    entry: Any, capped: int, exclude_slugs: frozenset[str] | set[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in entry.skills:
        if len(rows) >= capped:
            break
        compact = compact_row(raw)
        if compact is None or compact["slug"] in exclude_slugs:
            continue
        rows.append(compact)
    return rows


def federated_append(
    query: str | None,
    *,
    limit: int | None = None,
    exclude_slugs: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[dict[str, Any]], str]:
    """Return ``(compact_rows, freshness_flag)`` for ``query``.

    ``limit`` is clamped to ``[1, FEDERATED_MAX_CAP]``; ``None`` (the MCP wire
    default, and any caller that did not ask) means ``FEDERATED_DEFAULT_CAP``.

    Order: shared cache entry → (miss) live fan-out on a warm slot +
    local-index floor on a floor slot, collected within the budget. A stale
    entry is served at once and refreshed on a warm slot. Blocks at
    most ``live_budget_s()`` and NEVER raises: any unexpected failure degrades
    to ``([], "degraded")``, because a broken federated append must not take
    down the native search it rides on.
    """
    try:
        import time

        from app.services.metasearch_cache import get_cache

        requested = FEDERATED_DEFAULT_CAP if limit is None else int(limit)
        capped = max(1, min(requested, FEDERATED_MAX_CAP))
        q = query or ""
        cache = get_cache()
        budget = live_budget_s()
        sources = federated_sources()
        # Reads use _count=False: the hit-rate stats belong to the REST route.
        shared = cache.get_entry(q, sources, _count=False)
        flag = _STATE_TO_FLAG.get(shared.state, "degraded")
        if shared.entry is not None:
            if shared.state == "stale" and budget > 0:
                _run_bounded(_warm_slots, lambda: _warm_query(q, sources), "mcp-federated-refresh")
            return _rows_from_entry(shared.entry, capped, exclude_slugs), flag
        if budget <= 0:
            return [], flag

        t0 = time.monotonic()
        warm = _run_bounded(_warm_slots, lambda: _warm_query(q, sources), "mcp-federated-warm")
        floor = _run_bounded(
            _floor_slots,
            lambda: local_floor(query, limit=capped, exclude_slugs=exclude_slugs),
            "mcp-federated-floor",
        )
        if warm is not None and warm.done.wait(budget):
            again = cache.get_entry(q, sources, _count=False)
            if again.entry is not None:
                fresh_flag = "degraded" if again.state == "degraded" else "fresh"
                return _rows_from_entry(again.entry, capped, exclude_slugs), fresh_flag
        rows: list[dict[str, Any]] = []
        if floor is not None and floor.done.wait(max(0.0, budget - (time.monotonic() - t0))):
            rows = floor.result or []
        if flag == "degraded":
            return rows, "degraded"
        still_running = warm is not None and not warm.done.is_set()
        return rows, (WARMING if still_running else "cold")
    # Rationale: the federated append is best-effort garnish on the native
    # search — ANY failure in it (reader, decode, verdict) is reported as
    # degraded and never raised, or a cache hiccup would break search itself.
    except Exception:  # noqa: BLE001
        logger.warning("federated append failed; reporting degraded", exc_info=True)
        return [], "degraded"
