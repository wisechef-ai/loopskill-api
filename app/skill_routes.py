"""Skills list, detail, graph, related, and external routes.

Extracted from app/routes.py (Phase E — secfix_1905).

Registers:
  GET /skills/search           — full-text skill search
  GET /skills/trending         — trending by install count (with fallback widening)
  GET /skills/graph            — full marketplace skill graph
  GET /skills/{slug}           — skill detail (with body paywall)
  GET /skills/{slug}/external  — external_resources for a skill
  GET /skills/{slug}/related   — declared related skills
  GET /skills/{slug}/graph     — per-skill declared+derived graph edges

Also exports:
  search_skills, get_skill_detail, get_skill_external, trending_skills,
  get_full_skill_graph, get_skill_related, get_skill_graph
  (re-exportable from app.routes for backwards compat)
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app._skill_helpers import (
    GRAPH_RAIL_CAP,
    _hydrate_skill_outs,
    _install_counts_for,
    _resolve_related,
    _skill_to_out,
)
from app.database import get_db
from app.models import MissingSkillQuery, Skill, SkillAlias, TelemetryEvent
from app.schemas import SkillDetailOut, SkillOut, SkillSearchResult
from app.tier_labels import _is_paid_tier

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])

# WIS-903: Retired skill registry (loaded at import time, shared pattern)
from pathlib import Path as _Path

_RETIREMENT_FILE = _Path(__file__).resolve().parent.parent / "retired-skills.txt"
_RETIRED_SKILLS: dict[str, str] = {}
if _RETIREMENT_FILE.exists():
    for _line in _RETIREMENT_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#"):
            _parts = _line.split(None, 1)
            if len(_parts) == 2:
                _RETIRED_SKILLS[_parts[0]] = _parts[1]


@router.get("/skills/search", response_model=SkillSearchResult, tags=["skills"])
def search_skills(
    q: str | None = Query(None, description="Full-text search on title + description"),
    category: str | None = Query(None),
    vertical: str | None = Query(
        None,
        pattern="^(marketing|code|web-scraping|ops|sales|sim-robotics)$",
        description="Filter by Plan v5.4 vertical",
    ),
    tier: str | None = Query(
        None,
        pattern="^(free|pro|pro_plus|cook|operator|studio)$",  # cook|operator|studio = legacy aliases
        description="Filter by access tier (canonical: free|pro|pro_plus — legacy aliases cook|operator|studio accepted until 2026-06-10 via Phase A map)",
    ),
    subset: str | None = Query(
        None,
        pattern="^(pantry|menu|cookbook)$",
        description="v6: filter by catalog subset (pantry=original 3rd-party, menu=public custom, cookbook=private)",
    ),
    variant: str | None = Query(
        None, pattern="^(original|custom)$", description="v6: filter by skill_variant"
    ),
    sort: str = Query("updated_at", pattern="^(updated_at|created_at|title|quality_score)$"),
    min_quality: float | None = Query(
        None,
        ge=0,
        le=10,
        description="quality_1705 Phase C — filter skills with quality_score >= N. "
        "Skills without a computed quality_score are excluded when this is set.",
    ),
    hybrid: bool = Query(
        True,
        description="issue #111: when the literal keyword pass returns fewer than "
        "``hybrid_min_keyword_hits`` results, augment with hybrid recall "
        "(BM25 + vector) results. Set hybrid=false to force pure keyword.",
    ),
    hybrid_min_keyword_hits: int = Query(
        3,
        ge=0,
        le=20,
        description="Threshold below which hybrid fallback activates. Default 3.",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Full-text skill search with hybrid recall fallback."""
    query = (
        db.query(Skill)
        .options(
            joinedload(Skill.versions),
            joinedload(Skill.creator),
        )
        .filter(Skill.is_public == True, Skill.is_archived == False)
    )

    if q:
        query = query.filter((Skill.title.ilike(f"%{q}%")) | (Skill.description.ilike(f"%{q}%")))
    if category:
        query = query.filter(Skill.category == category)
    if vertical:
        query = query.filter(Skill.vertical == vertical)
    if tier:
        # Phase G post-drift-sweep (recipes_2005/G shipped 2026-05-20): the DB
        # column now holds canonical {free, pro, pro_plus}. Legacy {cook,
        # operator} input is still accepted as a 30-day READ alias (deprecation
        # window: 2026-06-10) so existing integrations keep working.
        tier_db = {"cook": "pro", "operator": "pro_plus", "studio": "pro_plus"}.get(
            tier, tier
        )  # legacy alias map
    # quality_1705 Phase C — quality_score floor filter for agent callers
    # who only want high-confidence skills. Skills without a score are
    # excluded (defensive: agent shouldn't pick a skill we haven't graded).
    if min_quality is not None:
        query = query.filter(Skill.quality_score >= min_quality)

    # v6 Phase A: subset filter — maps to skill_variant + is_public combinations
    if subset == "pantry":
        query = query.filter(Skill.skill_variant == "original")
    elif subset == "menu":
        query = query.filter(Skill.skill_variant == "custom", Skill.is_public == True)
    elif subset == "cookbook":
        # Private subset — currently empty in v6 Phase A (cookbook auto-fork ships in Phase B)
        query = query.filter(Skill.is_public == False)

    # v6 Phase A: variant filter (orthogonal to subset)
    if variant:
        query = query.filter(Skill.skill_variant == variant)

    # sort
    sort_col = getattr(Skill, sort, Skill.updated_at)
    query = query.order_by(sort_col.desc())

    total = query.count()
    results = query.offset((page - 1) * page_size).limit(page_size).all()

    keyword_skill_outs = []
    if results:
        # Issue #19: single batched query for all results instead of N per-row queries.
        counts = _install_counts_for(db, [s.id for s in results])
        keyword_skill_outs = [_skill_to_out(s, *counts.get(s.id, (0, 0))) for s in results]

    # issue #111: hybrid fallback for broad multi-keyword queries.
    # When the literal ILIKE pass returns fewer than ``hybrid_min_keyword_hits``
    # AND the caller supplied a non-empty query, augment with recall_skills
    # (BM25 + optional vector). This closes the "recall finds many, search
    # finds zero" gap reported by hermes-mac01.
    backend = "keyword"
    augmented = False
    final_outs = keyword_skill_outs
    final_total = total

    if hybrid and q and len(results) < hybrid_min_keyword_hits and page == 1:
        try:
            from app.recall_routes import recall_skills

            tier_for_recall: list[str] = ["free", "pro", "pro_plus"]
            if tier:
                # Caller asked for a specific tier — respect it.
                tier_db = {"cook": "pro", "operator": "pro_plus", "studio": "pro_plus"}.get(
                    tier, tier
                )  # legacy alias map
                tier_for_recall = [tier_db]

            recall_blob = recall_skills(
                db,
                query=q,
                tier_filter=tier_for_recall,
                limit=max(page_size, 10),
                user_id=None,
                is_master=True,
                user_tier=None,
            )
            recall_hits = recall_blob.get("hits", []) if isinstance(recall_blob, dict) else []

            # Map recall hits (slug-keyed) onto fresh Skill rows so we share the
            # same SkillOut shape with the literal pass. min_quality filter must
            # carry through so we don't smuggle low-quality rows back in via
            # recall.
            existing_slugs = {sk.slug for sk in results}
            extra_slugs = [
                h["slug"]
                for h in recall_hits
                if isinstance(h, dict) and h.get("slug") and h["slug"] not in existing_slugs
            ]

            if extra_slugs:
                extra_q = (
                    db.query(Skill)
                    .options(
                        joinedload(Skill.versions),
                        joinedload(Skill.creator),
                    )
                    .filter(
                        Skill.is_public == True,  # noqa: E712
                        Skill.is_archived == False,  # noqa: E712
                        Skill.slug.in_(extra_slugs),
                    )
                )
                # Re-apply the same hygiene + quality filters used above so we
                # don't widen the surface by accident.
                if min_quality is not None:
                    extra_q = extra_q.filter(Skill.quality_score >= min_quality)
                if category:
                    extra_q = extra_q.filter(Skill.category == category)
                if vertical:
                    extra_q = extra_q.filter(Skill.vertical == vertical)
                # Preserve recall's ranking order rather than the SQL default.
                extra_rows = {sk.slug: sk for sk in extra_q.all()}
                ordered_extras = [extra_rows[s] for s in extra_slugs if s in extra_rows]

                if ordered_extras:
                    extra_counts = _install_counts_for(db, [s.id for s in ordered_extras])
                    extra_outs = [_skill_to_out(s, *extra_counts.get(s.id, (0, 0))) for s in ordered_extras]
                    final_outs = (keyword_skill_outs + extra_outs)[:page_size]
                    final_total = total + len(extra_outs)
                    augmented = True
                    backend = "recall_only" if not results else "hybrid"
        # Rationale: hybrid recall failure must never degrade the keyword-literal search pass
        except Exception:  # noqa: BLE001 — never let hybrid kill the literal pass
            import logging

            logging.getLogger(__name__).exception(
                "hybrid search fallback failed; returning literal results only"
            )

    # topshelf_2605/H.1 — log zero-result queries for VOC digest.
    # Only fires when q is provided AND the final merged results list is empty
    # AND this is the first page (avoid double-counting paginated traversal).
    # Wrapped in try/except so a DB hiccup never breaks the search response.
    if q and not final_outs and page == 1:
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            today = date.today()
            bind = db.get_bind()
            if bind.dialect.name == "postgresql":
                # Postgres: atomic upsert — increment count on duplicate day.
                stmt = pg_insert(MissingSkillQuery).values(
                    query=q,
                    user_id=None,
                    day=today,
                    count=1,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        func.lower(MissingSkillQuery.query),
                        MissingSkillQuery.day,
                    ],
                    set_={"count": MissingSkillQuery.count + 1},
                )
                db.execute(stmt)
            else:
                # SQLite (tests): simple SELECT-then-upsert path.
                existing = (
                    db.query(MissingSkillQuery)
                    .filter(
                        func.lower(MissingSkillQuery.query) == q.lower(),
                        MissingSkillQuery.day == today,
                    )
                    .first()
                )
                if existing:
                    existing.count += 1
                else:
                    db.add(MissingSkillQuery(query=q, user_id=None, day=today, count=1))
            db.commit()
        # Rationale: VOC logging must never break the search response
        except Exception:  # noqa: BLE001
            logger.debug("missing_skill_query upsert failed — ignored", exc_info=True)
            db.rollback()

    return SkillSearchResult(
        results=final_outs,
        total=final_total,
        page=page,
        page_size=page_size,
        backend=backend,
        hybrid_augmented=augmented,
    )


@router.get("/skills/trending", response_model=SkillSearchResult, tags=["skills"])
def trending_skills(
    period: str = Query("week", pattern="^(day|week|month)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Trending = most telemetry install events in the given period.

    RCP-11: when the requested window has no install events, transparently
    widen the lookback (day → week → month → all-time) so a quiet stretch
    never returns empty trending while real install history exists. The
    response is the same shape; widening is silent on the wire and logged
    server-side so we can spot a chronically dead telemetry stream.
    """
    since_map = {"day": 1, "week": 7, "month": 30}
    fallback_chain = ["day", "week", "month", "all"]
    # Start the widening at (or after) the user's requested window.
    start_idx = fallback_chain.index(period)

    now = datetime.now(UTC)

    def _query_for(window: str):
        filters = [
            TelemetryEvent.event_type == "install",
            TelemetryEvent.skill_slug.isnot(None),
        ]
        if window != "all":
            filters.append(TelemetryEvent.created_at >= now - timedelta(days=since_map[window]))
        subq = (
            db.query(
                TelemetryEvent.skill_slug,
                func.count(TelemetryEvent.id).label("install_count"),
            )
            .filter(*filters)
            .group_by(TelemetryEvent.skill_slug)
            .subquery()
        )
        return (
            db.query(Skill)
            .options(joinedload(Skill.versions), joinedload(Skill.creator))
            .join(subq, Skill.slug == subq.c.skill_slug)
            .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
            .order_by(subq.c.install_count.desc())
        )

    query = None
    total = 0
    chosen_window = period
    for window in fallback_chain[start_idx:]:
        candidate = _query_for(window)
        candidate_total = candidate.count()
        if candidate_total > 0:
            query = candidate
            total = candidate_total
            chosen_window = window
            break

    if query is None:
        # Genuinely no install telemetry anywhere — return the empty shape.
        return SkillSearchResult(results=[], total=0, page=page, page_size=page_size)

    if chosen_window != period:
        logger.info(
            "trending: widened window %s → %s (no installs in requested period)",
            period,
            chosen_window,
        )

    results = query.offset((page - 1) * page_size).limit(page_size).all()

    counts = _install_counts_for(db, [s.id for s in results])
    return SkillSearchResult(
        results=[_skill_to_out(s, *counts.get(s.id, (0, 0))) for s in results],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/skills/graph", tags=["skills"])
def get_full_skill_graph(db: Session = Depends(get_db)):
    """Full marketplace skill graph dump for portal-side visualisation (Stage 3, G17).

    Returns ALL public skills as nodes plus all derived edges between them as
    undirected unique pairs. Single round-trip — designed for `/graph` page
    force-directed rendering.

    No auth required. Cheap query (≤200 nodes, ≤500 dedup edges in practice).
    """
    from app.models import SkillDerivedEdge

    public_skills = (
        db.query(Skill)
        .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
        .all()
    )
    public_slug_set = {s.slug for s in public_skills}

    nodes = [
        {
            "slug": s.slug,
            "title": s.title,
            "category": s.category or "general",
            "tier": s.tier or "pro",
            "install_count": int(s.install_count or 0),
        }
        for s in public_skills
    ]

    # Pull all directed edges, deduplicate to undirected, drop edges that
    # touch a non-public node (defence in depth — builder already filters).
    edge_rows = (
        db.query(SkillDerivedEdge.source_slug, SkillDerivedEdge.target_slug, SkillDerivedEdge.weight)
        .order_by(SkillDerivedEdge.weight.desc())
        .all()
    )
    seen: set[tuple[str, str]] = set()
    edges: list[dict] = []
    for src, tgt, w in edge_rows:
        if src not in public_slug_set or tgt not in public_slug_set:
            continue
        key = tuple(sorted([src, tgt]))
        if key in seen:
            continue
        seen.add(key)
        edges.append({"source": key[0], "target": key[1], "weight": float(w)})

    return {
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


@router.get("/skills/external", tags=["skills", "federation"])
def get_external_skills(
    request: Request,
    q: str | None = Query(None, description="Free-text query forwarded to each enabled source"),
    sources: str | None = Query(
        None,
        description=(
            "Comma-separated source ids to ENABLE (the free-source toggle). "
            "OFF BY DEFAULT: with no value, no external source is queried and the "
            "curated catalog stays clean. Live sources: hermes-hub, github-oss."
        ),
    ),
    limit: int = Query(20, ge=1, le=100),
    refresh: int = Query(
        0,
        ge=0,
        le=1,
        description="ADMIN ONLY: 1 forces a live re-walk + cache write for the queried sources.",
    ),
    db: Session = Depends(get_db),
):
    """evergreen_0206 Phase F2/F3 + superset_0606 Phase B — the live external
    (federated) catalog seam, now cache-backed.

    Surfaces external skills as a SEPARATE, second-class namespace
    ("External · community · as-is"), behind a per-source toggle that is OFF by
    default. Counts are reported INDEXED-vs-INSTALLABLE per source and never
    conflated (decision #5). Internal/private skills are NEVER surfaced here.

    superset_0606 Phase B — cache-backed counts:
      - A NON-enabled source reads its ``{indexed, installable, walked_at,
        stale}`` block from the PERSISTENT ``federation_index_cache`` table — a
        cold load NEVER triggers an inline cursor/sitemap walk (decision #7).
        Falls back to the cheap in-memory ``INDEXED_COUNT`` only when the source
        has no cache row yet (first boot before the reindex cron has run).
      - An ENABLED source still does a live, limited adapter search (the toggle
        is an explicit user action), and its fresh counts are written back to
        the cache so the next cold load is served from storage.
      - ``?refresh=1`` (admin only) forces a live re-walk + cache write.

    Toggle semantics:
      - ``sources`` empty/omitted  → every source disabled; ``external`` is [].
        Honest cached indexed counts are still reported per source.
      - ``sources=hermes-hub``     → only Hermes Hub queried + returned.
      - ``sources=hermes-hub,github-oss`` → both queried, merged, second-class.
    """
    from app.services import federation_cache as fcache
    from app.services.federation import ExternalSkill, LIVE_SOURCES, merge_search, route_install
    from app.services.federation_adapters import get_adapter
    from app.services.federation_live import LIVE_FETCH

    auth_ctx = getattr(request.state, "auth_ctx", None)
    caller_is_master = getattr(auth_ctx, "scope", None) == "master"
    force_refresh = bool(refresh) and caller_is_master

    requested = {s.strip().lower() for s in (sources or "").split(",") if s.strip()}
    # A source is "enabled" only if it is BOTH live and explicitly requested.
    enabled = [s for s in LIVE_SOURCES if s in requested]

    per_source: dict[str, dict] = {}
    all_external = []  # list[ExternalSkill] from enabled sources, in source order

    for source_id in LIVE_SOURCES:
        is_enabled = source_id in enabled
        block: dict = {
            "enabled": is_enabled,
            "indexed": None,
            "installable": None,
            "walked_at": None,
            "stale": None,
        }
        if is_enabled or (force_refresh and source_id in requested):
            has_query = bool((q or "").strip())
            existing = fcache.read_source_cache(db, source_id)
            existing_indexed = existing.get("indexed") if existing else None

            # superset_0606 Phase F — empty-query browse is served from the
            # cached first_page, NOT a live walk. The prod box shares ONE anon
            # GitHub budget (60/hr) across all users, so re-walking a facet on
            # every browse made facet/giant browse empty under any load. The
            # reindex cron already cached real rows; serve those. A live walk
            # happens ONLY for an actual query (q=...) or an admin force_refresh.
            served_from_cache = False
            if not has_query and not force_refresh and existing is not None:
                cached_rows = fcache.read_first_page(db, source_id)
                if cached_rows:
                    found = [ExternalSkill.from_dict(r) for r in cached_rows]
                    served_from_cache = True
                    # Report the canonical cached totals (not the first_page len).
                    block["indexed"] = existing_indexed
                    block["installable"] = existing.get("installable")
                    block["walked_at"] = existing.get("walked_at")
                    block["stale"] = existing.get("stale")
                    if is_enabled:
                        all_external.extend(found)

            if not served_from_cache:
                # Live, limited adapter search (query present / admin refresh /
                # first boot before the cron cached anything).
                fetch = LIVE_FETCH.get(source_id)
                adapter = get_adapter(source_id, fetch=fetch)
                try:
                    found = adapter.search(q or "", limit=limit) if adapter else []
                # Rationale: one bad source must never 500 the whole route.
                except Exception:  # noqa: BLE001
                    logger.warning("external source '%s' search failed", source_id, exc_info=True)
                    found = []
                installable = [s for s in found if route_install(s).allowed]
                block["indexed"] = len(found)
                block["installable"] = len(installable)
                if is_enabled:
                    all_external.extend(found)
                # superset_0606 Phase E — cache-write guard (decision #7 fix).
                #
                # The shipped behaviour wrote EVERY empty-query enabled search
                # back to the canonical cache. But an enabled toggle browse is
                # capped at ``limit`` (e.g. 50), so it would OVERWRITE the
                # reindex cron's real deep-walked counts (clawhub 69k, skills-sh
                # 20k) with the capped value — silently destroying the giants'
                # numbers. The portal's own 130-page build did exactly this.
                #
                # The canonical count is OWNED by the reindex cron (full walk).
                # The route may only:
                #   (a) write when force_refresh (admin explicit refresh), OR
                #   (b) SEED a source that has no cache row yet (first boot,
                #       before the cron's first run) — and even then never
                #       downgrade. ``found`` being exactly ``limit`` long means
                #       the result is truncated → a floor, never the real total.
                is_capped = len(found) >= limit
                may_seed = existing_indexed is None and not is_capped
                if force_refresh or (may_seed and not has_query):
                    try:
                        fcache.write_source_cache(
                            db,
                            source_id,
                            indexed_count=block["indexed"],
                            installable_count=block["installable"],
                            first_page=[s.to_dict() for s in found[:20]],
                            ttl_seconds=fcache.TTL_HOURLY,
                        )
                        cached = fcache.read_source_cache(db, source_id)
                        if cached:
                            block["walked_at"] = cached["walked_at"]
                            block["stale"] = cached["stale"]
                    except Exception:  # noqa: BLE001
                        logger.warning("federation cache write failed for %s", source_id, exc_info=True)
                elif existing is not None:
                    # Surface the canonical cached totals even on a live query —
                    # the capped live result is never the real indexed total.
                    block["indexed"] = existing_indexed
                    block["installable"] = existing.get("installable")
                    block["walked_at"] = existing.get("walked_at")
                    block["stale"] = existing.get("stale")
        else:
            # NOT enabled: read the honest cached block from the persistent
            # store ONLY — NEVER an inline walk or network call (decision #7).
            # A source with no cache row yet reports indexed=null ("not yet
            # walked"); the reindex cron fills it. We do NOT fall back to a live
            # network counter here — that would violate the zero-inline-walk
            # guarantee and make cold loads slow + flaky.
            cached = fcache.read_source_cache(db, source_id)
            if cached is not None:
                block["indexed"] = cached["indexed"]
                block["installable"] = cached["installable"]
                block["walked_at"] = cached["walked_at"]
                block["stale"] = cached["stale"]
        per_source[source_id] = block

    # The isolation wall: internal=[] (this surface is external-only); the toggle
    # is "on" iff at least one source is enabled. merge_search enforces that no
    # external rows leak when the toggle is off, and stamps the second-class
    # namespace + community-quality label on every external row.
    merged = merge_search([], all_external, free_sources_enabled=bool(enabled))
    payload = merged.to_dict()
    # Honest dual-count: sum of cached/live indexed across sources, omitting
    # null/failed sources (never fabricated). This is what the portal reads.
    payload["counts"]["external_indexed"] = sum(
        b["indexed"] for b in per_source.values() if isinstance(b.get("indexed"), int)
    )
    payload["counts"]["external_installable"] = sum(
        b["installable"] for b in per_source.values() if isinstance(b.get("installable"), int)
    )
    payload.update(
        {
            "query": q,
            "namespace": "external",
            "available_sources": list(LIVE_SOURCES),
            "enabled_sources": enabled,
            "per_source": per_source,
            "refreshed": force_refresh,
            "disclaimer": "External skills are community-contributed, as-is, and not quality-gated.",
        }
    )
    return payload


@router.get("/skills/external/{source}/{slug}/install", tags=["skills", "federation"])
def install_external_skill(source: str, slug: str, db: Session = Depends(get_db)):
    """evergreen_0206 Phase F2 — REAL fetch-origin install for an external skill.

    Closes the cold-path: makes the external install CTA actually work instead of
    being aspirational. The install ROUTER decides the path; this endpoint
    EXECUTES the redistributable one (fetch-origin) by streaming the real,
    MIT-licensed SKILL.md from origin, with license + attribution preserved.

    Returns, per the router's decision:
      - fetch_origin → {install_path, raw_url, content, license, install_command}
        (the agent writes ``content`` to its skills dir; the command is the
        copy-paste curl form for a human).
      - deep_link / non-redistributable → 409 with the origin link (never
        rehosted — license/ToS wall).
      - unknown source / unresolvable slug → 404 (honest, never fabricated).
    """
    from app.services import federation_cache as fcache
    from app.services.federation import ExternalSkill, INTERNAL_SOURCE, InstallPath, route_install
    from app.services.federation_adapters import get_adapter
    from app.services.federation_install import get_origin_fetcher
    from app.services.federation_live import LIVE_FETCH

    # The federation surface is external-only — refuse the internal namespace.
    if source == INTERNAL_SOURCE:
        raise HTTPException(status_code=404, detail="Not an external source")

    fetch = LIVE_FETCH.get(source)
    adapter = get_adapter(source, fetch=fetch)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"Unknown external source '{source}'")

    # superset_0606 Phase F — cache-first resolve. The prod box shares ONE anon
    # GitHub budget (60/hr) across all users, so a live adapter.resolve() (which
    # re-walks the tap to find the row) fails under load — exactly when a user
    # tries to install a facet skill they just browsed. The reindex cron already
    # cached the row in first_page; resolve from there first, falling back to a
    # live walk only when the slug isn't in the cached page (deep catalog).
    skill = None
    for row in fcache.read_first_page(db, source):
        if isinstance(row, dict) and row.get("slug") == slug:
            skill = ExternalSkill.from_dict(row)
            break

    if skill is None:
        try:
            skill = adapter.resolve(slug)
        # Rationale: a source outage must 503, not 500.
        except Exception:  # noqa: BLE001
            logger.warning("external resolve failed: %s/%s", source, slug, exc_info=True)
            raise HTTPException(status_code=503, detail="External source unavailable") from None
    if skill is None:
        raise HTTPException(status_code=404, detail=f"External skill '{slug}' not found in {source}")

    decision = route_install(skill)
    if not decision.allowed:
        # Deep-link / non-redistributable: never rehosted — hand back the origin.
        raise HTTPException(
            status_code=409,
            detail={
                "reason": decision.reason,
                "install_path": skill.install_path.value,
                "origin_url": skill.origin_url,
                "license": skill.license,
            },
        )

    if skill.install_path == InstallPath.FETCH_ORIGIN:
        origin_fetch = get_origin_fetcher(source)
        if origin_fetch is None:
            # FETCH_ORIGIN routed but no origin fetcher wired for this source yet
            # — honest 409 with the origin link rather than a fake body.
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": f"fetch-origin install not yet wired for source '{source}'",
                    "install_path": skill.install_path.value,
                    "origin_url": skill.origin_url,
                    "license": skill.license,
                },
            )
        # superset_0606 Phase F — pass the resolved skill's origin_url to the
        # fetcher (as a row) so the github-tap fetcher can derive the raw CDN URL
        # WITHOUT a live api.github.com walk. Fetchers that don't accept a row
        # (hermes/browse-sh/well-known/lobehub/skills-sh) ignore the kwarg via
        # the TypeError fallback — keeps the generic registry contract intact.
        fetch_row = {
            "slug": skill.slug,
            "origin_url": skill.origin_url,
            "source": skill.source,
        }
        try:
            got = origin_fetch(slug, row=fetch_row)
        except TypeError:
            got = origin_fetch(slug)
        if got is None:
            raise HTTPException(
                status_code=404,
                detail=f"SKILL.md for '{slug}' could not be fetched from origin",
            )
        raw_url, content = got
        # Mirror the source's home-dir layout. For namespaced slugs (host--task,
        # owner--repo) the leaf name is the human-facing skill name.
        leaf = slug.rsplit("--", 1)[-1]
        # spotify_0608 Ph E — provenance on the public external install. This is
        # a FETCH_ORIGIN install (real body streamed) so it's 'attributed'. We
        # materialize a private pointer Skill row (idempotent) to satisfy the
        # InstallEvent FK, then record + mint provenance. No cookbook context
        # here (cookbook_id stays NULL — this is the bare federation route).
        prov_id = None
        try:
            from app.services.cookbook_external import materialize_external_skill
            from app.services.provenance import (
                ATTR_ATTRIBUTED,
                record_install_with_provenance,
            )

            mat = materialize_external_skill(db, source, slug)
            if mat is not None:
                _ev, prov_id = record_install_with_provenance(
                    db,
                    skill=mat,
                    version_semver="external",
                    request=None,
                    source="external",
                    cookbook_id=None,
                    attribution=ATTR_ATTRIBUTED,
                )
                db.commit()
        # Rationale: provenance is best-effort observability on the public
        # federation route — a materialize/record hiccup must never block the
        # actual install (the agent still gets real content below).
        except Exception:  # noqa: BLE001
            logger.warning("external install provenance failed for %s/%s", source, slug, exc_info=True)
            db.rollback()
        return {
            "slug": skill.slug,
            "source": skill.source,
            "install_path": skill.install_path.value,
            "license": skill.license,
            "origin_url": skill.origin_url,
            "raw_url": raw_url,
            "content": content,
            "namespace": "external",
            "quality": "community · as-is",
            "provenance_id": prov_id,
            # Copy-paste form for a human; an agent uses `content` directly.
            "install_command": f"mkdir -p ~/.claude/skills/{leaf} && "
            f"curl -fsSL {raw_url} -o ~/.claude/skills/{leaf}/SKILL.md",
        }

    # Other allowed paths (e.g. register_mcp) have no file body to stream yet —
    # surface the routed decision honestly rather than pretend.
    # spotify_0608 Ph E — honest 'unattributed' provenance: a deep-link / non-fetch
    # install has no body, so we cannot attribute deeper than "this source/slug was
    # handed to an agent." We STILL mint a provenance_id (no hard-fail) mapping to
    # an InstallEvent stamped attribution='unattributed'. This is distinct from a
    # TRANSIENT FETCH_ORIGIN fetch failure (those 404/409 above and never reach here).
    prov_id = None
    try:
        from app.services.cookbook_external import materialize_external_skill
        from app.services.provenance import (
            ATTR_UNATTRIBUTED,
            record_install_with_provenance,
        )

        mat = materialize_external_skill(db, source, slug)
        if mat is not None:
            _ev, prov_id = record_install_with_provenance(
                db,
                skill=mat,
                version_semver="external",
                request=None,
                source="external",
                cookbook_id=None,
                attribution=ATTR_UNATTRIBUTED,
            )
            db.commit()
    # Rationale: provenance is best-effort observability; never block the honest
    # deep-link response on a provenance write hiccup.
    except Exception:  # noqa: BLE001
        logger.warning("deep-link provenance failed for %s/%s", source, slug, exc_info=True)
        db.rollback()
    return {
        "slug": skill.slug,
        "source": skill.source,
        "install_path": skill.install_path.value,
        "license": skill.license,
        "origin_url": skill.origin_url,
        "namespace": "external",
        "quality": "community · as-is",
        "provenance_id": prov_id,
        "attribution": "unattributed",
        "note": "This install path is not yet executable here; use the origin link.",
    }


@router.get("/skills/{slug}", response_model=SkillDetailOut, tags=["skills"])
def get_skill_detail(slug: str, request: Request, db: Session = Depends(get_db)):
    """Full skill detail with versions and resolved related skills.

    quality_1705 Phase B — body paywall:
      - Anonymous / free callers receive metadata-only (readme=null, external_resources=null)
      - Pro / Pro+ callers receive the full SKILL.md body + scripts/templates manifest
      - The master/admin api key is treated as Pro+ for self-test parity.
    """
    skill = (
        db.query(Skill)
        .options(joinedload(Skill.versions), joinedload(Skill.creator))
        .filter(Skill.slug == slug, Skill.is_public == True, Skill.is_archived == False)
        .first()
    )
    if not skill:
        # Phase J — check skill_aliases for a non-expired redirect.
        alias = db.query(SkillAlias).filter(SkillAlias.old_slug == slug).one_or_none()
        if alias is not None:
            now = datetime.now(UTC)
            expires = alias.expires_at
            # SQLite returns naive datetimes; treat naive as UTC for comparison.
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires is None or expires > now:
                return JSONResponse(
                    status_code=301,
                    headers={"Location": f"/api/skills/{alias.new_slug}"},
                    content={
                        "redirect_to": alias.new_slug,
                        "alias_expires_at": alias.expires_at.isoformat() if alias.expires_at else None,
                    },
                )
        # WIS-903: check retired skill registry
        _alt = _RETIRED_SKILLS.get(slug)
        if _alt:
            raise HTTPException(
                status_code=404,
                detail=f"This skill was retired 2026-05-07. See: {_alt} or contact support.",
            )
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    related_objs = _resolve_related(db, skill)
    counts = _install_counts_for(db, [skill.id])
    total_count, last_7d = counts.get(skill.id, (0, 0))

    # quality_1705 Phase B — body paywall.
    # Anonymous / free callers get metadata-only; Pro / Pro+ get the full body.
    # _is_paid_tier() resolves "cook" (legacy = Pro DB slug) AND "pro" / "pro_plus" to True.
    # Issue #25 (secfix_1905/H): auth_ctx.tier is set by APIKeyMiddleware (api-key path)
    # and by _auth_ctx_from_jwt_cookie (browser/cookie path for public skill-detail GETs).
    auth_ctx = getattr(request.state, "auth_ctx", None)
    caller_tier = auth_ctx.tier if auth_ctx is not None else None
    # Master scope (admin / ops key) bypasses the paywall — it carries no
    # subscription tier but must see the full body for self-test parity, as
    # documented in this handler's docstring. Bug B fix (repo-topclass P1):
    # the master key now actually reaches here because APIKeyMiddleware
    # resolves x-api-key on the skill-detail GET branch.
    caller_is_master = getattr(auth_ctx, "scope", None) == "master"
    caller_is_paid = caller_is_master or _is_paid_tier(caller_tier)
    # fix_2005: Free-tier skills are public by definition — their body must be
    # visible to anyone, including anonymous browser/Astro-build callers. The
    # paywall only applies to paid-tier skills (pro / pro_plus / legacy `cook`).
    # Without this gate, every free skill's portal page renders the Day-1
    # placeholder ("Detailed SKILL.md is being authored") instead of the actual
    # SKILL.md body, which masks the catalog's real content from public visitors.
    skill_is_free = skill.tier == "free"
    body_visible = skill_is_free or caller_is_paid
    readme_payload = skill.readme if body_visible else None
    external_payload = getattr(skill, "external_resources", None) if body_visible else None

    # polish_1805 hotfix — count of unhappy_paths entries in the readme YAML
    # frontmatter, computed server-side and exposed as a SCALAR (just a number,
    # no body content), so it is safe to surface even when readme_payload is
    # paywalled by Phase B. The static portal build uses it for the "N known
    # pitfalls documented" trust pill on every public skill page.
    unhappy_count = 0
    if skill.readme:
        _front = re.match(r"^---\s*\n(.*?)\n---\s*\n", skill.readme, re.DOTALL)
        if _front:
            _yaml = _front.group(1)
            _up = re.search(r"^unhappy_paths:\s*$", _yaml, re.MULTILINE)
            if _up:
                _tail = _yaml[_up.start() :]
                _rest = _tail[len("unhappy_paths:") :]
                _stop = re.search(r"^[a-z_][a-z0-9_]*:\s*$", _rest, re.MULTILINE)
                _block = _tail[: len("unhappy_paths:") + _stop.start()] if _stop else _tail
                unhappy_count = len(re.findall(r"^\s*-\s+condition:", _block, re.MULTILINE))

    return SkillDetailOut(
        id=skill.id,
        slug=skill.slug,
        title=skill.title,
        description=skill.description,
        category=skill.category,
        tier=skill.tier,
        is_public=skill.is_public,
        creator_name=skill.creator.name if skill.creator else None,
        creator_handle=skill.creator.handle if skill.creator else None,
        creator_url=skill.creator.url if skill.creator else None,
        latest_version=skill.versions[0].semver if skill.versions else None,
        install_count_total=total_count,
        install_count_7d=last_7d,
        readme=readme_payload,
        license=skill.license,
        # v6 Phase A catalog fields
        skill_variant=getattr(skill, "skill_variant", "custom") or "custom",
        original_source_url=getattr(skill, "original_source_url", None),
        parent_skill_slug=getattr(skill, "parent_skill_slug", None),
        pinned_sha=getattr(skill, "pinned_sha", None),
        upstream_status=getattr(skill, "upstream_status", "active") or "active",
        external_resources=external_payload,
        unhappy_paths_count=unhappy_count,
        versions=[
            {
                "id": v.id,
                "semver": v.semver,
                "changelog": v.changelog,
                "tarball_size_bytes": v.tarball_size_bytes,
                "checksum_sha256": v.checksum_sha256,
                "created_at": v.created_at,
            }
            for v in skill.versions
        ],
        related=related_objs,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        last_verified=getattr(skill, "last_verified", None),
        quality_score=getattr(skill, "quality_score", None),
    )


@router.get("/skills/{slug}/external", tags=["skills"])
def get_skill_external(slug: str, db: Session = Depends(get_db)):
    """v6 Phase A: Return external_resources JSON for a skill.

    Public, no auth — surfaces the "you might also want" upstream links the
    skill author declared in frontmatter. Empty list if none, 404 if skill
    missing, private, or archived (no oracle for private/archived slugs).

    Issue #16: added is_public + is_archived guard matching get_skill_detail.
    """
    skill = (
        db.query(Skill)
        .filter(
            Skill.slug == slug,
            Skill.is_public == True,  # noqa: E712
            Skill.is_archived == False,  # noqa: E712
        )
        .first()
    )
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    resources = getattr(skill, "external_resources", None) or []
    if not isinstance(resources, list):
        resources = []
    return resources


@router.get(
    "/skills/{slug}/related",
    response_model=list[SkillOut],
    tags=["skills"],
)
def get_skill_related(slug: str, db: Session = Depends(get_db)):
    """Return up to 10 public skills the author declared as related.

    Public — no auth required. Used by the portal "Works well with" rail
    and by the meta-skill v1.1+ install response.
    """
    skill = (
        db.query(Skill)
        .filter(Skill.slug == slug, Skill.is_public == True, Skill.is_archived == False)
        .first()
    )
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    return _resolve_related(db, skill)


@router.get("/skills/{slug}/graph", tags=["skills"])
def get_skill_graph(slug: str, db: Session = Depends(get_db)):
    """Return the Stage-1 declared edges + Stage-2 derived edges for a skill.

    Response shape:
        {
          "slug": str,
          "declared":  [SkillOut...],   # author-declared (Stage 1)
          "derived":   [SkillOut...],   # algorithm-derived (Stage 2), top-K
          "all":       [SkillOut...],   # union, declared first, capped at 10
          "edges":     [{slug, weight, signals}, ...]  # debug/derived metadata
        }
    """
    from app.models import SkillDerivedEdge

    skill = (
        db.query(Skill)
        .filter(Skill.slug == slug, Skill.is_public == True, Skill.is_archived == False)
        .first()
    )
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    declared = _resolve_related(db, skill)
    declared_slugs_set = {s["slug"] for s in declared}

    edge_rows = (
        db.query(SkillDerivedEdge)
        .filter(SkillDerivedEdge.source_slug == slug)
        .order_by(SkillDerivedEdge.weight.desc())
        .limit(GRAPH_RAIL_CAP * 2)  # over-fetch then filter for public/non-declared
        .all()
    )

    # Skip edges that target a non-public or already-declared skill
    derived_slugs: list[str] = []
    edge_meta: list[dict] = []
    for e in edge_rows:
        if e.target_slug in declared_slugs_set:
            continue
        if e.target_slug == slug:
            continue
        derived_slugs.append(e.target_slug)
        edge_meta.append(
            {
                "slug": e.target_slug,
                "weight": float(e.weight),
                "signals": e.signals or {},
            }
        )
        if len(derived_slugs) >= GRAPH_RAIL_CAP:
            break

    derived = _hydrate_skill_outs(db, derived_slugs)

    # Union: declared first (handcrafted), then derived (algorithmic)
    seen = set()
    union = []
    for s in declared + derived:
        if s["slug"] in seen:
            continue
        seen.add(s["slug"])
        union.append(s)
        if len(union) >= GRAPH_RAIL_CAP:
            break

    return {
        "slug": slug,
        "declared": declared,
        "derived": derived,
        "all": union,
        "edges": edge_meta,
    }
