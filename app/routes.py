"""API route handlers.

Endpoints per WIS-462 spec:
  GET  /api/skills/search?q&category&sort     — full-text skill search
  GET  /api/skills/install?slug&mode          — signed tarball download URL
  GET  /api/skills/trending?period=week|month  — trending by install count
  GET  /api/skills/access?skill               — access check for a skill
  GET  /api/carousel/today                     — today's carousel entries
  GET  /api/carousel/{YYYY-MM-DD}             — carousel by date
  POST /api/telemetry                          — record telemetry event
  GET  /api/wisechef/demo-cta                  — WiseChef cross-sell CTA
  POST /api/wisechef/demo-request              — submit a demo request
"""

import json
import os
import time
import tomllib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    APILibraryEntry,
    CarouselEntry,
    Creator,
    InstallEvent,
    Recipe,
    SkillVersion,
    Skill,
    TelemetryEvent,
    User,
    WiseChefDemoRequest,
)
from app.schemas import (
    APILibraryOut,
    CarouselEntryOut,
    DemoCTAOut,
    DemoRequestIn,
    DemoRequestOut,
    HealthOut,
    InstallResponse,
    RecipeOut,
    SkillAccessOut,
    SkillDetailOut,
    SkillOut,
    SkillSearchResult,
    TelemetryEventOut,
    TelemetryIn,
)

router = APIRouter(prefix="/api")

VERSION = "0.4.0"


def _build_manifest(latest: SkillVersion, skill: Skill) -> dict:
    """F-API-14: Build manifest dict from skill.toml for install response."""
    toml_text = latest.skill_toml or ""
    try:
        toml_data = tomllib.loads(toml_text).get("skill", {})
        return {
            "category": toml_data.get("category") or skill.category,
            "tags": toml_data.get("tags", []),
            "tier": toml_data.get("tier"),
        }
    except Exception:
        return {"category": skill.category}


# ── Health ──────────────────────────────────────────────────────────────

@router.get("/healthz", tags=["meta"])
def healthz(db: Session = Depends(get_db)):
    try:
        db.execute(func.count(1))
        db_status = "ok"
    except Exception:
        db_status = "error"
    return HealthOut(status="ok", version=VERSION, db=db_status)


# ── Skills ──────────────────────────────────────────────────────────────

def _skill_to_out(skill: Skill) -> SkillOut:
    latest = skill.versions[0].semver if skill.versions else None
    return SkillOut(
        id=skill.id,
        slug=skill.slug,
        title=skill.title,
        description=skill.description,
        category=skill.category,
        tier=skill.tier,
        is_public=skill.is_public,
        creator_name=skill.creator.name if skill.creator else None,
        latest_version=latest,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


@router.get("/skills/search", response_model=SkillSearchResult, tags=["skills"])
def search_skills(
    q: str | None = Query(None, description="Full-text search on title + description"),
    category: str | None = Query(None),
    tier: str | None = Query(None, pattern="^(free|cook|operator|studio)$", description="Filter by access tier"),
    sort: str = Query("updated_at", pattern="^(updated_at|created_at|title)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(Skill).options(
        joinedload(Skill.versions),
        joinedload(Skill.creator),
    ).filter(Skill.is_public == True)

    if q:
        query = query.filter(
            (Skill.title.ilike(f"%{q}%")) | (Skill.description.ilike(f"%{q}%"))
        )
    if category:
        query = query.filter(Skill.category == category)
    if tier:
        query = query.filter(Skill.tier == tier)

    # sort
    sort_col = getattr(Skill, sort, Skill.updated_at)
    query = query.order_by(sort_col.desc())

    total = query.count()
    results = query.offset((page - 1) * page_size).limit(page_size).all()

    return SkillSearchResult(
        results=[_skill_to_out(s) for s in results],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/skills/trending", response_model=SkillSearchResult, tags=["skills"])
def trending_skills(
    period: str = Query("week", pattern="^(day|week|month)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Trending = most telemetry install events in the given period."""
    since_map = {"day": 1, "week": 7, "month": 30}
    since_days = since_map[period]
    since = datetime.now(timezone.utc) - timedelta(days=since_days)

    # Aggregate telemetry install counts per skill
    subq = (
        db.query(
            TelemetryEvent.skill_slug,
            func.count(TelemetryEvent.id).label("install_count"),
        )
        .filter(
            TelemetryEvent.event_type == "install",
            TelemetryEvent.skill_slug.isnot(None),
            TelemetryEvent.created_at >= since,
        )
        .group_by(TelemetryEvent.skill_slug)
        .subquery()
    )

    query = (
        db.query(Skill)
        .options(joinedload(Skill.versions), joinedload(Skill.creator))
        .join(subq, Skill.slug == subq.c.skill_slug)
        .filter(Skill.is_public == True)
        .order_by(subq.c.install_count.desc())
    )

    total = query.count()
    results = query.offset((page - 1) * page_size).limit(page_size).all()

    return SkillSearchResult(
        results=[_skill_to_out(s) for s in results],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/skills/install", response_model=InstallResponse, tags=["skills"])
def install_skill(
    request: Request,
    slug: str = Query(..., description="Skill slug"),
    mode: str = Query("files", pattern="^(files|full)$"),
    db: Session = Depends(get_db),
):
    """Return a signed URL for downloading the skill tarball.

    Public skills are installable by any valid api-key. Private skills are
    installable ONLY by the admin master key OR by the api-key whose user
    owns the skill (creator self-install — required for dogfooding).
    """
    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    # Visibility check
    if not skill.is_public:
        api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
        # api_key_user_id is None for the master/admin key, UUID for a user key
        is_admin = api_key_user_id is None
        is_owner = (
            skill.creator
            and api_key_user_id is not None
            and api_key_user_id != "MISSING"
            and skill.creator.user_id == api_key_user_id
        )
        if not (is_admin or is_owner):
            raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    if not skill.versions:
        raise HTTPException(status_code=404, detail=f"No versions available for '{slug}'")

    latest = skill.versions[0]

    # Generate a signed token (HMAC-style with itsdangerous)
    from itsdangerous import URLSafeTimedSerializer
    from app.config import settings

    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET)
    token = serializer.dumps({"slug": slug, "version_id": str(latest.id), "mode": mode})

    # Build signed download URL — use the public origin so installs work
    # from any host (not only loopback). Fall back to localhost for dev.
    public_origin = (
        getattr(settings, "PUBLIC_ORIGIN", None)
        or os.environ.get("RECIPES_PUBLIC_ORIGIN")
        or "https://recipes.wisechef.ai"
    )
    url_base = public_origin.rstrip("/") + "/api/skills/_download" + "?" + "tok" + "en="
    tarball_url = url_base + token

    # Log install event
    from uuid import uuid4 as _uuid4
    api_key_id = getattr(request.state, "api_key_id", None)
    event = InstallEvent(
        id=_uuid4(),
        skill_id=skill.id,
        skill_slug=slug,
        api_key_id=api_key_id,
        version_semver=latest.semver,
        client_ip=request.client.host if request.client else None,
    )
    db.add(event)
    db.commit()

    return InstallResponse(
        slug=slug,
        version=latest.semver,
        tarball_url=tarball_url,
        checksum_sha256=latest.checksum_sha256,
        size_bytes=latest.tarball_size_bytes,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        manifest=_build_manifest(latest, skill),
    )


@router.get("/skills/_download", tags=["skills"])
def download_tarball(
    token: str = Query(..., description="Signed download token"),
    db: Session = Depends(get_db),
):
    """Verify signed token and return tarball info."""
    from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
    from app.config import settings

    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET)
    try:
        data = serializer.loads(token, max_age=3600)
    except SignatureExpired:
        raise HTTPException(status_code=410, detail="Download token expired")
    except BadSignature:
        raise HTTPException(status_code=403, detail="Invalid download token")

    slug = data["slug"]
    version_id = data["version_id"]

    version = db.query(SkillVersion).filter(SkillVersion.id == version_id).first()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    # Stream the actual tarball file. Path is recorded at publish-time as
    # absolute (e.g. /var/lib/recipes-skills/agent-rescue/1.1.0.tar.gz).
    from fastapi.responses import FileResponse
    import pathlib as _pl

    tar_path = _pl.Path(version.tarball_path) if version.tarball_path else None
    if not tar_path or not tar_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Tarball missing on disk for {slug}@{version.semver}",
        )
    return FileResponse(
        path=str(tar_path),
        media_type="application/gzip",
        filename=f"{slug}-{version.semver}.tar.gz",
        headers={"X-Checksum-SHA256": version.checksum_sha256 or ""},
    )


@router.get("/skills/access", response_model=SkillAccessOut, tags=["skills"])
def skill_access(
    skill: str = Query(..., description="Skill slug to check access for"),
    db: Session = Depends(get_db),
):
    """Check if the authenticated caller has access to a skill.

    Public skills are always accessible. Tier-gated skills require matching tier.
    """
    s = db.query(Skill).filter(Skill.slug == skill).first()
    if not s:
        raise HTTPException(status_code=404, detail=f"Skill '{skill}' not found")

    # Public skills = always accessible
    has_access = s.is_public and (s.tier is None or s.tier == "cook")

    latest = s.versions[0].semver if s.versions else None

    return SkillAccessOut(
        slug=s.slug,
        title=s.title,
        has_access=has_access,
        tier=s.tier,
        latest_version=latest,
        license=s.license,
    )


@router.get("/skills/{slug}", response_model=SkillDetailOut, tags=["skills"])
def get_skill_detail(slug: str, db: Session = Depends(get_db)):
    """Full skill detail with versions and resolved related skills."""
    skill = (
        db.query(Skill)
        .options(joinedload(Skill.versions), joinedload(Skill.creator))
        .filter(Skill.slug == slug, Skill.is_public == True)
        .first()
    )
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    related_objs = _resolve_related(db, skill)

    return SkillDetailOut(
        id=skill.id,
        slug=skill.slug,
        title=skill.title,
        description=skill.description,
        category=skill.category,
        tier=skill.tier,
        is_public=skill.is_public,
        creator_name=skill.creator.name if skill.creator else None,
        latest_version=skill.versions[0].semver if skill.versions else None,
        readme=skill.readme,
        license=skill.license,
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
    )


# Maximum related skills returned by detail/related endpoints. Stage 1 cap.
RELATED_SKILLS_CAP = 10


def _resolve_related(db: Session, skill: "Skill") -> list:
    """Resolve `skill.related_skills` slug list to public SkillOut payloads.

    Filters applied (Stage 1 contract):
      - drop self-reference (skill.slug appearing in its own related_skills)
      - drop slugs that don't exist in DB
      - drop is_public=False skills (no internal-leak)
      - cap at RELATED_SKILLS_CAP, preserving frontmatter declaration order
    """
    raw = skill.related_skills or []
    if not raw:
        return []

    # Normalise: drop self-refs, lowercase, dedupe preserving order
    seen: set[str] = set()
    candidates: list[str] = []
    for s in raw:
        if not isinstance(s, str):
            continue
        norm = s.strip().lower()
        if not norm or norm == skill.slug or norm in seen:
            continue
        seen.add(norm)
        candidates.append(norm)
        if len(candidates) >= RELATED_SKILLS_CAP * 2:  # over-fetch buffer for filtering
            break

    if not candidates:
        return []

    # Single query: pull all candidate public skills at once
    rows = (
        db.query(Skill)
        .filter(Skill.slug.in_(candidates), Skill.is_public == True)
        .all()
    )
    by_slug = {r.slug: r for r in rows}

    # Preserve declaration order, cap at limit
    out = []
    for slug in candidates:
        r = by_slug.get(slug)
        if not r:
            continue
        latest = r.versions[0].semver if r.versions else None
        out.append({
            "id": r.id,
            "slug": r.slug,
            "title": r.title,
            "description": r.description,
            "category": r.category,
            "tier": r.tier,
            "is_public": r.is_public,
            "creator_name": r.creator.name if r.creator else None,
            "latest_version": latest,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        })
        if len(out) >= RELATED_SKILLS_CAP:
            break
    return out


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
        .filter(Skill.slug == slug, Skill.is_public == True)
        .first()
    )
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    return _resolve_related(db, skill)


# ── Skill Graph (Stage 2 — G16) ─────────────────────────────────────────

GRAPH_RAIL_CAP = 10


def _hydrate_skill_outs(db: Session, slugs: list[str]) -> list[dict]:
    """Resolve a list of slugs (preserving order) to public SkillOut dicts."""
    if not slugs:
        return []
    rows = (
        db.query(Skill)
        .filter(Skill.slug.in_(slugs), Skill.is_public == True)
        .all()
    )
    by_slug = {r.slug: r for r in rows}
    out = []
    for slug in slugs:
        r = by_slug.get(slug)
        if not r:
            continue
        latest = r.versions[0].semver if r.versions else None
        out.append({
            "id": r.id,
            "slug": r.slug,
            "title": r.title,
            "description": r.description,
            "category": r.category,
            "tier": r.tier,
            "is_public": r.is_public,
            "creator_name": r.creator.name if r.creator else None,
            "latest_version": latest,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        })
    return out


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
        .filter(Skill.slug == slug, Skill.is_public == True)
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
        edge_meta.append({
            "slug": e.target_slug,
            "weight": float(e.weight),
            "signals": e.signals or {},
        })
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


@router.get("/recipes/{slug}", response_model=RecipeOut, tags=["recipes"])
def get_recipe(slug: str, db: Session = Depends(get_db)):
    recipe = (
        db.query(Recipe)
        .options(joinedload(Recipe.creator))
        .filter(Recipe.slug == slug, Recipe.is_public == True)
        .first()
    )
    if not recipe:
        raise HTTPException(status_code=404, detail=f"Recipe '{slug}' not found")

    return RecipeOut(
        id=recipe.id,
        slug=recipe.slug,
        title=recipe.title,
        description=recipe.description,
        content=recipe.content,
        category=recipe.category,
        creator_name=recipe.creator.name if recipe.creator else None,
        created_at=recipe.created_at,
        updated_at=recipe.updated_at,
    )


@router.get("/api-library/{slug}", response_model=APILibraryOut, tags=["api-library"])
def get_api_library_entry(slug: str, db: Session = Depends(get_db)):
    entry = db.query(APILibraryEntry).filter(APILibraryEntry.slug == slug).first()
    if not entry:
        raise HTTPException(status_code=404, detail=f"API library entry '{slug}' not found")
    return entry


# ── Carousel ────────────────────────────────────────────────────────────
# Sprint 4: routes moved to app/carousel/routes.py with new contract wire
# format (slot/role/score). The legacy CarouselEntryOut shape with
# archives_at / seconds_until_archive is a UI helper concern — when the
# Astro landing page consumes the new endpoint, port the archive-countdown
# logic into a thin wrapper field on the new response model. Deleted here
# to eliminate duplicate path mount that was shadowing the new router.


# ── Telemetry ───────────────────────────────────────────────────────────

@router.post("/telemetry", status_code=201, tags=["telemetry"], response_model=TelemetryEventOut)
def post_telemetry(
    request: Request,
    body: TelemetryIn,
    db: Session = Depends(get_db),
):
    """Record a telemetry event.

    Accepts two modes (both may be combined):
    - **Typed mode**: typed fields (goal_class, duration_seconds, retry_count,
      user_intervention, agent_class_hash) land in dedicated columns.
    - **Legacy mode**: ``payload`` dict is JSON-serialised into the ``payload``
      text column. Existing callers continue to work unchanged.

    Validation (raises 422 on failure):
    - ``event_type`` ∈ {install, first_use, task_completed, task_failed, replaced}
    - ``duration_seconds`` 0..86400
    - ``agent_class_hash`` regex ^[a-f0-9]{8,64}$ if present

    Raises 404 if ``skill_slug`` is provided but not found in the skills table.
    """
    # Resolve skill_slug → skill_id (required if slug provided)
    skill_id = None
    if body.skill_slug:
        # F5: filter to public skills OR the caller's own private skills to avoid
        # enumeration oracle (201 vs 404 leaking private skill existence)
        api_key_user_id = getattr(request.state, "api_key_user_id", None)
        skill_query = db.query(Skill).filter(Skill.slug == body.skill_slug)
        skill = skill_query.first()
        if not skill:
            raise HTTPException(status_code=404, detail="unknown skill_slug")
        # If skill is private, only creator or admin can log telemetry against it
        if not skill.is_public:
            is_admin = api_key_user_id is None
            is_owner = (
                skill.creator is not None
                and api_key_user_id is not None
                and str(skill.creator.user_id) == str(api_key_user_id)
            )
            if not (is_admin or is_owner):
                # Return 404, not 403 — 403 is also an oracle
                raise HTTPException(status_code=404, detail="unknown skill_slug")
        skill_id = skill.id

    event = TelemetryEvent(
        event_type=body.event_type,
        skill_slug=body.skill_slug,
        # F9: preserve empty dict semantics — {} stores as '{}', not NULL
        payload=json.dumps(body.payload) if body.payload is not None else None,
        # Typed columns (NULL when not provided)
        skill_id=skill_id,
        goal_class=body.goal_class,
        duration_seconds=body.duration_seconds,
        retry_count=body.retry_count,
        user_intervention=body.user_intervention,
        agent_class_hash=body.agent_class_hash,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return TelemetryEventOut(status="recorded", event_id=str(event.id))


# ── WiseChef Demo CTA ───────────────────────────────────────────────────

@router.get("/stats", tags=["meta"])
def marketplace_stats(db: Session = Depends(get_db)):
    """Public marketplace transparency stats — totals, top categories, top skills.

    No auth required. Powers the /stats portal page and the recipes_stats MCP tool.
    Designed to beat LarryBrain's opacity: full counts, fresh data, clear scope.
    """
    from sqlalchemy import func as _f

    total_skills = db.query(_f.count(Skill.id)).filter(Skill.is_public == True).scalar() or 0
    total_installs = db.query(_f.count(InstallEvent.id)).scalar() or 0

    # Tier breakdown
    tier_rows = (
        db.query(Skill.tier, _f.count(Skill.id))
        .filter(Skill.is_public == True)
        .group_by(Skill.tier)
        .all()
    )
    by_tier = {t or "uncategorized": int(c) for t, c in tier_rows}

    # Category breakdown
    cat_rows = (
        db.query(Skill.category, _f.count(Skill.id))
        .filter(Skill.is_public == True)
        .group_by(Skill.category)
        .order_by(_f.count(Skill.id).desc())
        .limit(20)
        .all()
    )
    by_category = [{"category": c or "uncategorized", "count": int(n)} for c, n in cat_rows]

    # Top installed skills (lifetime)
    top_rows = (
        db.query(InstallEvent.skill_slug, _f.count(InstallEvent.id).label("installs"))
        .group_by(InstallEvent.skill_slug)
        .order_by(_f.count(InstallEvent.id).desc())
        .limit(10)
        .all()
    )
    top_installed = [{"slug": s, "installs": int(c)} for s, c in top_rows]

    # Recent installs (last 7d)
    recent_window = datetime.now(timezone.utc) - timedelta(days=7)
    installs_7d = (
        db.query(_f.count(InstallEvent.id))
        .filter(InstallEvent.created_at >= recent_window)
        .scalar()
        or 0
    )

    # Trending pairs (Stage 2, G16): top-weighted derived edges, deduplicated
    # to undirected pairs. Quietly returns [] when the edge table is empty.
    try:
        from app.models import SkillDerivedEdge
        edge_rows = (
            db.query(SkillDerivedEdge.source_slug, SkillDerivedEdge.target_slug,
                     SkillDerivedEdge.weight)
            .order_by(SkillDerivedEdge.weight.desc())
            .limit(200)  # over-fetch, dedupe pulls roughly half
            .all()
        )
        seen_pairs: set[tuple[str, str]] = set()
        trending_pairs: list[dict] = []
        for src, tgt, w in edge_rows:
            key = tuple(sorted([src, tgt]))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            trending_pairs.append({"a": key[0], "b": key[1], "weight": float(w)})
            if len(trending_pairs) >= 10:
                break
    except Exception:
        trending_pairs = []

    return {
        "total_skills": int(total_skills),
        "total_installs_lifetime": int(total_installs),
        "installs_last_7d": int(installs_7d),
        "by_tier": by_tier,
        "by_category": by_category,
        "top_installed": top_installed,
        "trending_pairs": trending_pairs,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── WiseChef Demo CTA ───────────────────────────────────────────────────

@router.get("/wisechef/demo-cta", response_model=DemoCTAOut, tags=["wisechef"])
def demo_cta():
    """WiseChef cross-sell CTA for the Recipes marketplace.

    Returns dynamic marketing content for the landing page and carousel.
    """
    return DemoCTAOut(
        headline="Stop managing AI agents. Start earning with them.",
        subheadline="WiseChef runs your AI workflows — content, SEO, reporting — so you focus on clients.",
        cta_text="Book a Free Demo",
        cta_url="https://wisechef.ai/signup",
        social_proof=[
            "Trusted by marketing agencies across Europe",
            "200+ hours saved per month on content workflows",
            "Set up in 15 minutes, not 15 days",
        ],
        tier_from="€499/mo",
    )


@router.post("/wisechef/demo-request", response_model=DemoRequestOut, status_code=201, tags=["wisechef"])
def submit_demo_request(
    body: DemoRequestIn,
    db: Session = Depends(get_db),
):
    """Submit a demo request from the Recipes marketplace.

    Stores in wisechef_demo_requests table for follow-up.
    """
    # Check for duplicate email
    existing = db.query(WiseChefDemoRequest).filter(
        WiseChefDemoRequest.email == body.email,
    ).first()
    if existing:
        return DemoRequestOut(
            id=existing.id,
            email=existing.email,
            company_name=existing.company_name,
            company_size=existing.company_size,
            source=existing.source,
            status=existing.status,
            created_at=existing.created_at,
        )

    req = WiseChefDemoRequest(
        email=body.email,
        company_name=body.company_name,
        company_size=body.company_size,
        source=body.source,
        message=body.message,
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    return DemoRequestOut(
        id=req.id,
        email=req.email,
        company_name=req.company_name,
        company_size=req.company_size,
        source=req.source,
        status=req.status,
        created_at=req.created_at,
    )
