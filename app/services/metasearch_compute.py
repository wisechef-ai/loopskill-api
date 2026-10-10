"""The ONE unified-metasearch compute, shared by the REST route and MCP search.

Before this module the compute lived as a closure inside
``metasearch_routes.metasearch``, deliberately not importable, because the MCP
search path was forbidden to fan out (a cold fan-out once measured >90s). That
cost came from ClawHub owner lookups — one live HTTP call per row — and was
removed by ``prime_clawhub_owner_cache`` (issue #148) and by ClawHub search hits
carrying ``ownerHandle`` inline. Re-measured on prod 2026-10-04: a cold fan-out
over every source finishes in ~2s (sources run in parallel, each under its
own deadline: ``metasearch_fanout.deadline_for``).

What the cache-only rule cost in exchange: ``loopskill_search`` answered
``federated: cold`` with ZERO federated rows for every query no REST caller had
warmed in the last 15 minutes. Verified live: ``loopskill_search("ste100")``
returned nothing while ``/api/skills/metasearch?q=ste100`` returned 30 rows.

Contract (fed1004, after three adversarial review rounds):

- ONE cache key per query and ONE per-source deadline for every caller, so a
  cold query fans out once per process no matter which surface asks first, and
  no caller ever waits longer than the web UI's own compute.
- A source slower than ITS deadline is dropped from THAT compute and demoted
  by its breaker, exactly as before. ClawHub's live search measured 1.6-2.1s
  from prod on 2026-10-05, so under the old shared 1.2s budget it was degraded
  on nearly every cold compute (t_b9887867); the live-search sources now carry
  their own budget. ClawHub COVERAGE still also comes from the hourly hub
  snapshot (79k ClawHub rows, searched locally by the ``hermes-hub`` source).
  A late-merge of stragglers was built and deleted in review: it could
  overwrite a newer cache generation and the breaker defeated it anyway.
- A background compute uses its OWN database session.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app._skill_helpers import _install_counts_for, _skill_to_out
from app.models import Skill

logger = logging.getLogger(__name__)

CURATED_CAP = 50  # curated candidates pulled before the merge caps the page


def curated_candidates(db: Session, q: str | None, limit: int) -> list[dict]:
    """Pull curated (internal, public) skill rows matching the query, as
    _skill_to_out dicts. Mirrors the literal-match pass of /api/skills/search but
    only the public catalog (the federation wall: never surface private skills)."""
    query = (
        db.query(Skill)
        .options(joinedload(Skill.versions), joinedload(Skill.creator))
        .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
    )
    if q:
        like = f"%{q}%"
        query = query.filter(
            Skill.title.ilike(like)
            | Skill.description.ilike(like)
            | Skill.category.ilike(like)
            | Skill.readme.ilike(like)
        )
    rows = query.limit(limit).all()
    if not rows:
        return []
    counts = _install_counts_for(db, [s.id for s in rows])
    out = []
    for s in rows:
        skill_out = _skill_to_out(s, *counts.get(s.id, (0, 0)))
        d = skill_out.model_dump() if hasattr(skill_out, "model_dump") else dict(skill_out)
        # unify_curated reads install_count + slug/title/description/updated_at
        d["install_count"] = d.get("install_count_total", 0)
        out.append(d)
    return out


def build_unified(db: Session, q: str | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Build the unified ranked result against ``db``.

    Returns ``(contracted_skills, sources_ok, sources_degraded)`` — the exact
    triple ``HotQueryCache.get_or_compute`` stores. Module attributes are read at call
    time (``_fanout.fan_out``) so a test patch on the fan-out module is honoured
    here exactly as it is in the route.
    """
    from app.services import metasearch_fanout as _fanout
    from app.services.clawhub_owner_prime import prime_clawhub_owner_cache
    from app.services.metasearch import unify_curated, unify_external

    curated = [unify_curated(r) for r in curated_candidates(db, q, CURATED_CAP)]
    # issue #148: seed the ClawHub owner cache from the persisted snapshot BEFORE
    # fanning out, so no browse row pays a live owner lookup on a worker thread.
    prime_clawhub_owner_cache(db)
    fanout = _fanout.fan_out(q or "", sources=_fanout.DEFAULT_FANOUT_SOURCES)
    external = _unify_pairs(unify_external, fanout.pairs)
    sources_ok = ["recipes", *fanout.sources_ok]
    merged = _merge(q, curated, external, sources_ok, list(fanout.sources_degraded))
    if merged[0]:
        return merged
    return _relaxed_or(merged, q, sources_ok, list(fanout.sources_degraded))


RELAXED_CAP = 30


def _relaxed_or(
    strict: tuple[list[dict[str, Any]], list[str], list[str]],
    q: str | None,
    sources_ok: list[str],
    sources_degraded: list[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """ah_1010: a 3+ subject-word query that every source answered with zero
    rows retries against the local hub index with ONE word allowed to miss.

    Every 2-word subset of "postgres index advisor" returned rows; the 3-word
    query returned none, because each source requires every word (or the whole
    phrase). Each relaxed card carries ``relaxed: True`` so a caller can tell a
    near match from an exact one. ``merge_unified`` ranks the cards by word
    coverage (``query_coverage``), so rows covering more of the query lead.

    The strict (empty) result stands when the query is not relaxable, when the
    index has no near match, or when the index read fails, because a relaxed
    pass must never turn a search into an error.
    """
    from app.services.hub_local_search import search_hub_index_relaxed
    from app.services.metasearch import unify_external

    try:
        near = search_hub_index_relaxed(q, limit=RELAXED_CAP)
    # Rationale: the relaxed pass is a second chance on an already-empty answer;
    # a DB hiccup here degrades to that empty answer, never to a 500.
    except Exception:  # noqa: BLE001
        logger.warning("relaxed hub pass failed for %r", q, exc_info=True)
        return strict
    if not near:
        return strict
    external = _unify_pairs(unify_external, [(skill, None) for skill in near])
    skills, ok, degraded = _merge(q, [], external, sources_ok, sources_degraded)
    return [{**card, "relaxed": True} for card in skills], ok, degraded


def _merge(
    q: str | None, curated: list, external: list, sources_ok: list[str], sources_degraded: list[str]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    from app.services.metasearch import merge_unified
    from app.services.metasearch_card_contract import apply_card_contract

    payload = merge_unified(
        curated, external, query=q, sources_ok=sources_ok, sources_degraded=sources_degraded
    ).to_dict()
    return (
        apply_card_contract(payload["skills"]),
        payload.get("sources_ok", []),
        payload.get("sources_degraded", []),
    )


def _unify_pairs(unify_external: Any, pairs: Any) -> list:
    out = []
    for skill, raw in pairs:
        unified = _safe_unify(unify_external, skill, raw)
        if unified is not None:
            out.append(unified)
    return out


def build_unified_own_session(q: str | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """``build_unified`` on a fresh session — for any compute that may outlive
    the request that started it (SWR refresh, MCP warm)."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return build_unified(db, q)
    finally:
        db.close()


def _safe_unify(unify_external: Any, skill: Any, raw: Any) -> Any:
    """One external row → UnifiedSkill, or None when the row is malformed.

    A single upstream row with a wrong-typed field (a list where a title
    belongs) used to raise inside the merge and fail the WHOLE compute — every
    source's rows lost for one bad hit (fed1004 R1). The row is dropped and
    logged instead; the other rows ship. ``origin_url`` and ``description`` may
    be None: the card contract normalises both, and a fetch-origin row without
    a page URL is still installable.
    """
    try:
        unified = unify_external(skill, raw_row=raw)
    # Rationale: one malformed upstream row must never fail the whole fan-out.
    except Exception:  # noqa: BLE001
        logger.warning(
            "dropping malformed federated row from %s", getattr(skill, "source", "?"), exc_info=True
        )
        return None
    for field in ("slug", "title", "source", "install_ref"):
        if not isinstance(getattr(unified, field, None), str):
            logger.warning(
                "dropping federated row with non-string %s from %s", field, getattr(unified, "source", "?")
            )
            return None
    for field in ("origin_url", "description"):
        if not isinstance(getattr(unified, field, None), (str, type(None))):
            logger.warning(
                "dropping federated row with non-string %s from %s", field, getattr(unified, "source", "?")
            )
            return None
    return unified


def warm(q: str | None, sources: tuple[str, ...]) -> None:
    """Fill or refresh the cache entry for ``(q, sources)`` ON THE CALLING THREAD.

    - fresh entry → nothing to do;
    - stale entry → ``refresh_now``: synchronous, per-key guarded, CAS-stored;
    - miss → ``get_or_compute``: synchronous single-flight with any other
      caller of the same key in this process.

    Synchronous on purpose: a caller that bounds its concurrency (MCP warm
    slots) holds its slot for exactly as long as the fan-out runs.
    """
    from app.services.metasearch_cache import get_cache

    cache = get_cache()

    def _compute() -> tuple[list[dict[str, Any]], list[str], list[str]]:
        return build_unified_own_session(q)

    lookup = cache.get_entry(q or "", sources, _count=False)
    if lookup.entry is not None and lookup.entry.fresh:
        return
    if lookup.entry is not None and lookup.entry.stale:
        cache.refresh_now((q or "", sources), _compute, expected_seq=lookup.entry.seq)
        return
    cache.get_or_compute((q or "", sources), _compute, refresh_fn=_compute)
