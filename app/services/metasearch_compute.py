"""The ONE unified-metasearch compute, shared by the REST route and MCP search.

Before this module the compute lived as a closure inside
``metasearch_routes.metasearch``, deliberately not importable, because the MCP
search path was forbidden to fan out (a cold fan-out once measured >90s). That
cost came from ClawHub owner lookups — one live HTTP call per row — and was
removed by ``prime_clawhub_owner_cache`` (issue #148) and by ClawHub search hits
carrying ``ownerHandle`` inline. Re-measured on prod 2026-10-04: a cold fan-out
over every source finishes in ~2s (per-source deadline 1.2s, run in parallel).

What the cache-only rule cost in exchange: ``loopskill_search`` answered
``federated: cold`` with ZERO federated rows for every query no REST caller had
warmed in the last 15 minutes — that is, for nearly every first question an
agent asks. Verified live: ``loopskill_search("ste100")`` returned nothing while
``/api/skills/metasearch?q=ste100`` returned 30 rows, and the identical MCP call
returned them the moment the REST call had warmed the cache.

This module gives both surfaces the same compute:

- the REST route calls ``build_unified`` with the fan-out's default (web UI)
  per-source deadline, under its own cache key;
- MCP calls ``warm`` with a longer deadline under ITS own key (``mcp_cache_
  sources``), so a REST request never waits behind a slower MCP compute (fed1004
  R1); MCP still reads the REST key first;
- a background compute uses its OWN database session — the request session is
  closed by the time a slow fan-out finishes.
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


def build_unified(
    db: Session, q: str | None, *, per_source_deadline_s: float | None = None
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Build the unified ranked result against ``db``.

    ``per_source_deadline_s`` None keeps the fan-out's own default (sized for
    the web UI's render budget). A caller with a larger budget — MCP search —
    passes a longer deadline so slower sources (ClawHub search: p50 ~1.25s from
    prod) are not cut off.

    Returns ``(contracted_skills, sources_ok, sources_degraded)`` — the exact
    triple ``HotQueryCache.get_or_compute`` stores. Module attributes are read
    at call time (``_fanout.fan_out``) so a test patch on the fan-out module is
    honoured here exactly as it is in the route.
    """
    from app.services import metasearch_fanout as _fanout
    from app.services.clawhub_owner_prime import prime_clawhub_owner_cache
    from app.services.metasearch import merge_unified, unify_curated, unify_external
    from app.services.metasearch_card_contract import apply_card_contract

    curated = [unify_curated(r) for r in curated_candidates(db, q, CURATED_CAP)]
    # issue #148: seed the ClawHub owner cache from the persisted snapshot BEFORE
    # fanning out, so no browse row pays a live owner lookup on a worker thread.
    prime_clawhub_owner_cache(db)
    deadline = {} if per_source_deadline_s is None else {"per_source_deadline_s": per_source_deadline_s}
    fanout = _fanout.fan_out(q or "", sources=_fanout.DEFAULT_FANOUT_SOURCES, **deadline)
    external = []
    for skill, raw in fanout.pairs:
        unified = _safe_unify(unify_external, skill, raw)
        if unified is not None:
            external.append(unified)
    result = merge_unified(
        curated,
        external,
        query=q,
        sources_ok=["recipes", *fanout.sources_ok],
        sources_degraded=fanout.sources_degraded,
    )
    payload = result.to_dict()
    contracted = apply_card_contract(payload["skills"])
    return contracted, payload.get("sources_ok", []), payload.get("sources_degraded", [])


def build_unified_own_session(
    q: str | None, *, per_source_deadline_s: float | None = None
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """``build_unified`` on a fresh session — for any compute that may outlive
    the request that started it (SWR refresh, MCP background warm)."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return build_unified(db, q, per_source_deadline_s=per_source_deadline_s)
    finally:
        db.close()


def _safe_unify(unify_external: Any, skill: Any, raw: Any) -> Any:
    """One external row → UnifiedSkill, or None when the row is malformed.

    A single upstream row with a wrong-typed field (a list where a title
    belongs) used to raise inside the merge and fail the WHOLE compute — every
    source's rows lost for one bad hit (fed1004 R1). The row is dropped and
    logged instead; the other rows ship.
    """
    try:
        unified = unify_external(skill, raw_row=raw)
    # Rationale: one malformed upstream row must never fail the whole fan-out.
    except Exception:  # noqa: BLE001
        logger.warning(
            "dropping malformed federated row from %s", getattr(skill, "source", "?"), exc_info=True
        )
        return None
    for field in ("slug", "title", "source", "origin_url", "install_ref"):
        if not isinstance(getattr(unified, field, None), str):
            logger.warning("dropping federated row with non-string %s from %s", field, unified.source)
            return None
    if not isinstance(getattr(unified, "description", ""), (str, type(None))):
        return None
    return unified


def warm(q: str | None, sources: tuple[str, ...], *, per_source_deadline_s: float | None = None) -> None:
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
        return build_unified_own_session(q, per_source_deadline_s=per_source_deadline_s)

    lookup = cache.get_entry(q or "", sources, _count=False)
    if lookup.entry is not None and lookup.entry.fresh:
        return
    if lookup.entry is not None and lookup.entry.stale:
        cache.refresh_now((q or "", sources), _compute, expected_seq=lookup.entry.seq)
        return
    cache.get_or_compute((q or "", sources), _compute, refresh_fn=_compute)
