"""Cache-ONLY federated append for the MCP search path (unisearch_0709 P2).

The problem, verified live 2026-09-08: ``loopskill_search("graft")`` returned
``{"results": [], "total": 0}`` while the metasearch fan-out found
``skills-sh:trailhq--graft--graft`` and ``loopskill_install`` installed it fine.
Discovery disagreed with install, so every MCP-facing agent concluded LoopSkill
has nothing on any federated topic.

This module is the seam that fixes it, and its whole design is one sentence:
**read the shared cache, never compute.**

Three rules it exists to hold, none of which the MCP tool has to restate:

1. **Bounded, never unbounded.** (fed1004 — supersedes "never a live fan-out".)
   The cache is read first, in microseconds. On a MISS this module starts ONE
   single-flight fan-out through the same compute and cache entry the REST route
   uses (``metasearch_compute.warm``) and waits for it at most
   ``settings.MCP_FEDERATED_LIVE_BUDGET_S`` (default 4s; a cold fan-out measured
   ~2s on prod 2026-10-04). Finished in time → the fresh rows. Not finished →
   rows from the LOCAL hub index (``federation_hub_skills``, a DB query, no
   network) flagged ``warming``, while the fan-out completes in the background
   and warms the cache for the next call. Budget 0 restores the old cache-only
   behaviour. The old rule existed because a cold fan-out once took >90s; that
   cost was ClawHub owner lookups (fixed by issue #148), and keeping the rule
   after the cost was gone made every first-time query answer ``cold`` with
   zero federated rows — ``loopskill_search("ste100")`` found nothing while the
   REST metasearch found 30 skills.
2. **Honest freshness.** ``fresh`` / ``stale`` / ``cold`` / ``degraded`` map
   1:1 onto the reader's own states; ``warming`` means "a live fan-out is still
   running — these rows come from the local index, ask again in a few seconds
   for the full set". A Redis outage is ``degraded``, never an empty-but-fresh
   answer and never an exception.
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

# Live warms that may run at once in this worker. A burst of distinct cold
# queries must not become a burst of parallel fan-outs (each one already runs
# up to 8 source threads); past this, a cold query is answered from the local
# index alone and flagged ``cold``.
MAX_CONCURRENT_WARMS = 4
_warm_slots = threading.BoundedSemaphore(MAX_CONCURRENT_WARMS)

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
    return {
        "slug": _text(row.get("slug"), _MAX_SLUG_LEN),
        "title": _text(row.get("title") or row.get("slug"), _MAX_TITLE_LEN),
        "install_ref": install_ref,
        "deployable": _deployable_verdict(row),
        "install_path": _text(row.get("install_path"), _MAX_SHORT_LEN),
        "origin_url": _text(row.get("origin_url"), _MAX_URL_LEN),
        "quality": _text(row.get("quality"), _MAX_SHORT_LEN),
    }


# Per-source fan-out deadline for an MCP warm, kept inside the wait budget with
# room for the curated query + merge. The REST route keeps the fan-out default
# (1.2s, sized for the web UI's 1.5s render budget); an agent can wait longer,
# and ClawHub's search route answers in ~1.0-1.5s from prod (measured
# 2026-10-04), so the UI deadline would cut it off on most cold queries.
_MCP_SOURCE_DEADLINE_MAX_S = 3.0
_MCP_SOURCE_DEADLINE_MIN_S = 1.2
_MERGE_MARGIN_S = 0.75


def mcp_source_deadline_s(budget_s: float) -> float:
    """Per-source deadline for a warm started under ``budget_s``."""
    return max(_MCP_SOURCE_DEADLINE_MIN_S, min(_MCP_SOURCE_DEADLINE_MAX_S, budget_s - _MERGE_MARGIN_S))


def _warm_query(query: str, sources: tuple[str, ...]) -> None:
    """The live fan-out. A module attribute so tests can swap it; production
    goes through the single-flight cache entry shared with the REST route."""
    from app.services.metasearch_compute import warm

    warm(query, sources, per_source_deadline_s=mcp_source_deadline_s(live_budget_s()))


def live_budget_s() -> float:
    """The live-fan-out wait budget, clamped to ``[0, _MAX_LIVE_BUDGET_S]``."""
    from app.config import settings

    try:
        budget = float(getattr(settings, "MCP_FEDERATED_LIVE_BUDGET_S", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(budget, _MAX_LIVE_BUDGET_S))


def _start_warm(
    query: str, sources: tuple[str, ...], warm_fn: Callable[[str, tuple[str, ...]], None]
) -> threading.Event | None:
    """Run ``warm_fn`` on a daemon thread; return its done-Event, or None when
    every warm slot is busy (the caller then answers without a live fan-out).
    The thread never raises into anything: a failed warm only means the next
    call is cold again."""
    if not _warm_slots.acquire(blocking=False):
        return None
    done = threading.Event()

    def _run() -> None:
        try:
            warm_fn(query, sources)
        # Rationale: a background warm is best-effort; its failure must never
        # surface anywhere except the log.
        except Exception:  # noqa: BLE001
            logger.warning("federated live warm failed for %r", query, exc_info=True)
        finally:
            _warm_slots.release()
            done.set()

    threading.Thread(target=_run, name="mcp-federated-warm", daemon=True).start()
    return done


def local_floor(
    query: str | None, *, limit: int, exclude_slugs: frozenset[str] | set[str]
) -> list[dict[str, Any]]:
    """Compact rows from the LOCAL hub index — no network, one DB query.

    ``federation_hub_skills`` holds the ingested Hermes Hub snapshot (~100k
    skills across skills.sh, ClawHub, GitHub, LobeHub, browse.sh and the
    official set). It is what an agent gets while a live fan-out is still
    running, so a first-time query is never empty when the index knows the
    answer. Empty query → no floor (a bare catalog listing is the native pass's
    job).
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        from app.services.federation_adapters import get_adapter
        from app.services.metasearch import unify_external

        hub = get_adapter("hermes-hub")
        skills = hub.search(q, limit=max(limit * 2, 10)) if hub is not None else []
        rows: list[dict[str, Any]] = []
        for skill in skills:
            if len(rows) >= limit:
                break
            compact = compact_row(unify_external(skill).to_dict())
            if compact is None or compact["slug"] in exclude_slugs:
                continue
            rows.append(compact)
        return rows
    # Rationale: the floor is a fallback for a fallback — a DB hiccup here
    # degrades to "no floor rows", never to a failed search.
    except Exception:  # noqa: BLE001
        logger.warning("federated local floor failed for %r", q, exc_info=True)
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

    Order: cache read → (miss) bounded live fan-out → (still running) local
    index floor. A stale hit is served at once and refreshed in the background.
    Blocks at most ``live_budget_s()`` and NEVER raises: any unexpected failure
    degrades to ``([], "degraded")``, because a broken federated append must not
    take down the native search it rides on.
    """
    try:
        from app.services.metasearch_cache import get_cache

        requested = FEDERATED_DEFAULT_CAP if limit is None else int(limit)
        capped = max(1, min(requested, FEDERATED_MAX_CAP))
        sources = federated_sources()
        q = query or ""
        cache = get_cache()
        lookup = cache.get_entry(q, sources)
        flag = _STATE_TO_FLAG.get(lookup.state, "degraded")
        if lookup.entry is not None:
            if lookup.state == "stale":
                _start_warm(q, sources, _warm_query)  # SWR: serve now, refresh behind
            return _rows_from_entry(lookup.entry, capped, exclude_slugs), flag

        budget = live_budget_s()
        if budget <= 0:
            return [], flag
        done = _start_warm(q, sources, _warm_query)
        if done is not None and done.wait(budget):
            again = cache.get_entry(q, sources)
            if again.entry is not None:
                fresh_flag = "degraded" if again.state == "degraded" else "fresh"
                return _rows_from_entry(again.entry, capped, exclude_slugs), fresh_flag
        floor = local_floor(query, limit=capped, exclude_slugs=exclude_slugs)
        still_running = done is not None and not done.is_set()
        return floor, (WARMING if still_running else flag)
    # Rationale: the federated append is best-effort garnish on the native
    # search — ANY failure in it (reader, decode, verdict) is reported as
    # degraded and never raised, or a cache hiccup would break search itself.
    except Exception:  # noqa: BLE001
        logger.warning("federated append failed; reporting degraded", exc_info=True)
        return [], "degraded"
