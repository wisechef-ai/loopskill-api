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
from datetime import UTC, datetime, timedelta

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
from app.models import Skill, SkillAlias, TelemetryEvent
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
        pattern="^(free|pro|pro_plus|cook|operator|studio)$",
        description="Filter by access tier (DB: free|cook|operator|studio; display: free|pro|pro_plus — accepted as aliases via Phase A map)",
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
        # Phase A — top1pct_1105: accept display slugs `pro` / `pro_plus` and
        # transparently map them to the immutable DB slugs `cook` / `operator`.
        # The DB column stays stable (per locked decision in plan §0); user-facing
        # filters use the new brand labels.
        tier_db = {"pro": "cook", "pro_plus": "operator"}.get(tier, tier)
        query = query.filter(Skill.tier == tier_db)

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

            tier_for_recall: list[str] = ["free", "cook", "operator"]
            if tier:
                # Caller asked for a specific tier — respect it.
                tier_db = {"pro": "cook", "pro_plus": "operator"}.get(tier, tier)
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
        except Exception:  # noqa: BLE001 — never let hybrid kill the literal pass
            import logging

            logging.getLogger(__name__).exception(
                "hybrid search fallback failed; returning literal results only"
            )

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
            "tier": s.tier or "cook",
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
    caller_is_paid = _is_paid_tier(caller_tier)
    readme_payload = skill.readme if caller_is_paid else None
    external_payload = getattr(skill, "external_resources", None) if caller_is_paid else None

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
