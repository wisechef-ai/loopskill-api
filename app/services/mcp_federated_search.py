"""Cache-ONLY federated append for the MCP search path (unisearch_0709 P2).

The problem, verified live 2026-09-08: ``loopskill_search("graft")`` returned
``{"results": [], "total": 0}`` while the metasearch fan-out found
``skills-sh:trailhq--graft--graft`` and ``loopskill_install`` installed it fine.
Discovery disagreed with install, so every MCP-facing agent concluded LoopSkill
has nothing on any federated topic.

This module is the seam that fixes it, and its whole design is one sentence:
**read the shared cache, never compute.**

Three rules it exists to hold, none of which the MCP tool has to restate:

1. **Never a live fan-out on the MCP thread.** It calls P1's cache-ONLY reader
   (``HotQueryCache.get_entry``) and nothing else — never ``get_or_compute``,
   never ``fan_out``, never the metasearch route's ``_build`` closure (which is
   deliberately non-importable). A cold worker answers ``cold`` in microseconds.
   The alternative was measured: a cold fan-out on prod took >90s, which every
   agent reads as "LoopSkill is broken".
2. **Honest freshness.** ``fresh`` / ``stale`` / ``cold`` / ``degraded`` map
   1:1 onto the reader's own states. A Redis outage is ``degraded``, never an
   empty-but-fresh answer and never an exception.
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
from typing import Any

logger = logging.getLogger(__name__)

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


def federated_append(
    query: str | None,
    *,
    limit: int | None = None,
    exclude_slugs: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[dict[str, Any]], str]:
    """Return ``(compact_rows, freshness_flag)`` for ``query`` — cache ONLY.

    ``limit`` is clamped to ``[1, FEDERATED_MAX_CAP]``; ``None`` (the MCP wire
    default, and any caller that did not ask) means ``FEDERATED_DEFAULT_CAP``.

    Never computes, never fans out, never blocks and NEVER raises: any
    unexpected failure degrades to ``([], "degraded")``, because a broken
    federated append must not take down the native search it rides on.
    """
    try:
        from app.services.metasearch_cache import get_cache

        requested = FEDERATED_DEFAULT_CAP if limit is None else int(limit)
        capped = max(1, min(requested, FEDERATED_MAX_CAP))
        lookup = get_cache().get_entry(query or "", federated_sources())
        flag = _STATE_TO_FLAG.get(lookup.state, "degraded")
        if lookup.entry is None:
            return [], flag

        rows: list[dict[str, Any]] = []
        for raw in lookup.entry.skills:
            if len(rows) >= capped:
                break
            compact = compact_row(raw)
            if compact is None or compact["slug"] in exclude_slugs:
                continue
            rows.append(compact)
        return rows, flag
    # Rationale: the federated append is best-effort garnish on the native
    # search — ANY failure in it (reader, decode, verdict) is reported as
    # degraded and never raised, or a cache hiccup would break search itself.
    except Exception:  # noqa: BLE001
        logger.warning("federated append failed; reporting degraded", exc_info=True)
        return [], "degraded"
