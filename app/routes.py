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
import logging
import os
import time
import tomllib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import case, func
from sqlalchemy.orm import Session, joinedload

logger = logging.getLogger(__name__)

from app.database import get_db
from app.tier_labels import display_label
from app.models import (
    APIKey,
    APILibraryEntry,
    CarouselEntry,
    Creator,
    InstallEvent,
    Recipe,
    SkillAlias,
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

# WIS-903: Retired skill registry
from pathlib import Path as _Path
_RETIREMENT_FILE = _Path(__file__).resolve().parent.parent / 'retired-skills.txt'
_RETIRED_SKILLS: dict[str, str] = {}
if _RETIREMENT_FILE.exists():
    for _line in _RETIREMENT_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith('#'):
            _parts = _line.split(None, 1)
            if len(_parts) == 2:
                _RETIRED_SKILLS[_parts[0]] = _parts[1]


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

def _skill_to_out(
    skill: Skill,
    install_count_total: int = 0,
    install_count_7d: int = 0,
) -> SkillOut:
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
        install_count_total=install_count_total,
        install_count_7d=install_count_7d,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


def _install_counts_for(db: Session, skill_ids: list) -> dict:
    """Return {skill_id: (total, last_7d)} for the supplied skill ids.

    One round-trip aggregation — small marketplace (≤200 skills) so a
    grouped query is cheaper than a LATERAL per row.
    """
    if not skill_ids:
        return {}
    since_7d = datetime.now(timezone.utc) - timedelta(days=7)
    rows = (
        db.query(
            InstallEvent.skill_id,
            func.count(InstallEvent.id).label("total"),
            func.sum(
                case((InstallEvent.created_at >= since_7d, 1), else_=0)
            ).label("last_7d"),
        )
        .filter(InstallEvent.skill_id.in_(skill_ids))
        .group_by(InstallEvent.skill_id)
        .all()
    )
    return {sid: (int(total or 0), int(last_7d or 0)) for sid, total, last_7d in rows}


@router.get("/skills/search", response_model=SkillSearchResult, tags=["skills"])
def search_skills(
    q: str | None = Query(None, description="Full-text search on title + description"),
    category: str | None = Query(None),
    vertical: str | None = Query(None, pattern="^(marketing|code|web-scraping|ops|sales|sim-robotics)$",
                                  description="Filter by Plan v5.4 vertical"),
    tier: str | None = Query(None, pattern="^(free|pro|pro_plus|cook|operator|studio)$", description="Filter by access tier (DB: free|cook|operator|studio; display: free|pro|pro_plus — accepted as aliases via Phase A map)"),
    subset: str | None = Query(None, pattern="^(pantry|menu|cookbook)$",
                                description="v6: filter by catalog subset (pantry=original 3rd-party, menu=public custom, cookbook=private)"),
    variant: str | None = Query(None, pattern="^(original|custom)$",
                                 description="v6: filter by skill_variant"),
    sort: str = Query("updated_at", pattern="^(updated_at|created_at|title)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(Skill).options(
        joinedload(Skill.versions),
        joinedload(Skill.creator),
    ).filter(Skill.is_public == True, Skill.is_archived == False)

    if q:
        query = query.filter(
            (Skill.title.ilike(f"%{q}%")) | (Skill.description.ilike(f"%{q}%"))
        )
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

    counts = _install_counts_for(db, [s.id for s in results])
    return SkillSearchResult(
        results=[
            _skill_to_out(s, *counts.get(s.id, (0, 0))) for s in results
        ],
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

    now = datetime.now(timezone.utc)

    def _query_for(window: str):
        filters = [
            TelemetryEvent.event_type == "install",
            TelemetryEvent.skill_slug.isnot(None),
        ]
        if window != "all":
            filters.append(
                TelemetryEvent.created_at >= now - timedelta(days=since_map[window])
            )
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
            .filter(Skill.is_public == True)  # noqa: E712
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
        results=[
            _skill_to_out(s, *counts.get(s.id, (0, 0))) for s in results
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


# ── marketing_1205: UTM ref attribution ─────────────────────────────────────
_UTM_REF_ALLOWLIST = frozenset({"li", "x", "yt", "ig", "fb", "agentpact"})
_UTM_COOKIE_NAME = "recipes_utm_ref"
_UTM_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days in seconds


def _set_utm_ref_cookie(response, ref: str | None) -> None:
    """Set httpOnly UTM ref cookie if ref is on the allowlist; silently drop others."""
    if ref and ref in _UTM_REF_ALLOWLIST:
        response.set_cookie(
            _UTM_COOKIE_NAME,
            value=ref,
            max_age=_UTM_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=True,
        )


@router.get("/skills/install", response_model=InstallResponse, tags=["skills"])
def install_skill(
    request: Request,
    slug: str = Query(..., description="Skill slug; supports 'slug@semver' suffix"),
    mode: str = Query("files", pattern="^(files|full)$"),
    version: str | None = Query(
        None,
        description="Pin install to a specific semver. Overrides any '@version' suffix on slug.",
    ),
    ref: str | None = Query(None, description="UTM ref platform code (li, x, yt, ig, fb, agentpact)"),
    db: Session = Depends(get_db),
):
    """Return a signed URL for downloading the skill tarball.

    Public skills are installable by any valid api-key. Private skills are
    installable ONLY by the admin master key OR by the api-key whose user
    owns the skill (creator self-install — required for dogfooding).
    """
    # Stream 4: support 'slug@semver' inline pinning, or explicit ?version=
    if "@" in slug and version is None:
        slug, _v = slug.split("@", 1)
        version = (_v or "").strip() or None
    slug = slug.strip()
    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if not skill:
        # WIS-903: check retired skill registry
        _alt = _RETIRED_SKILLS.get(slug)
        if _alt:
            raise HTTPException(
                status_code=404,
                detail=f"This skill was retired 2026-05-07. See: {_alt} or contact support.",
            )
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

    # WIS-902: Tier-aware install rate limit
    caller_tier = _resolve_caller_tier_for_install(db, request)
    install_limit = TIER_INSTALL_LIMITS.get(caller_tier, 5)
    api_key_id = getattr(request.state, "api_key_id", None)
    
    if install_limit is not None:  # None = unlimited
        today_count = _count_today_installs(db, api_key_id)
        if today_count >= install_limit:
            remaining = 0
            reset_at = (datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1)).isoformat()
            
            from fastapi.responses import JSONResponse as _JRP
            return _JRP(
                status_code=429,
                content={
                    "detail": f"Install rate limit exceeded ({install_limit}/day for {caller_tier or 'free'} tier). "
                              f"Upgrade to {display_label('pro_plus')} for unlimited installs.",
                    "tier": caller_tier,
                    "limit": install_limit,
                    "remaining": remaining,
                    "reset_at": reset_at,
                },
                headers={
                    "X-RateLimit-Limit": str(install_limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": reset_at,
                    "Retry-After": str(int((datetime.now(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ) + timedelta(days=1) - datetime.now(timezone.utc)).total_seconds())),
                },
            )

    if not skill.versions:
        raise HTTPException(status_code=404, detail=f"No versions available for '{slug}'")

    # Stream 4: explicit version pinning. None ⇒ latest (existing behaviour).
    if version:
        target = next((v for v in skill.versions if v.semver == version), None)
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Version '{version}' not found for '{slug}'. "
                    f"Available: {[v.semver for v in skill.versions]}"
                ),
            )
        latest = target
    else:
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

    # RCP-13: keep the denormalised Skill.install_count counter in sync with
    # the InstallEvent table (the path /api/skills/install actually writes).
    # Atomic SQL-level expression so concurrent installs cannot lose writes.
    # Same transaction as the InstallEvent insert — either both land or
    # neither does.
    db.query(Skill).filter(Skill.id == skill.id).update(
        {Skill.install_count: Skill.install_count + 1},
        synchronize_session=False,
    )
    db.commit()

    # WIS-902: Add rate-limit info headers to successful response
    resp_headers = {}
    if install_limit is not None:
        today_count_after = _count_today_installs(db, api_key_id)
        remaining = max(0, install_limit - today_count_after)
        resp_headers["X-RateLimit-Limit"] = str(install_limit)
        resp_headers["X-RateLimit-Remaining"] = str(remaining)
    
    resp = InstallResponse(
        slug=slug,
        version=latest.semver,
        tarball_url=tarball_url,
        checksum_sha256=latest.checksum_sha256,
        size_bytes=latest.tarball_size_bytes,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        manifest=_build_manifest(latest, skill),
    )
    if resp_headers or ref:
        from fastapi.responses import JSONResponse as _JR
        json_resp = _JR(content=resp.model_dump(mode="json"), headers=resp_headers)
        _set_utm_ref_cookie(json_resp, ref)
        return json_resp
    return resp


# ── marketing_1205: platform short-link redirectors ─────────────────────────
# X (and some other platforms) strip query params from short links,
# so we provide /x/<slug>, /li/<slug> etc. that 302 to /api/skills/install?slug=<slug>&ref=<platform>.
# These are root-level routes (no /api prefix) — use a separate router.

from fastapi.responses import RedirectResponse as _RedirectResponse  # noqa: E402
from fastapi import APIRouter as _APIRouter  # noqa: E402

utm_router = _APIRouter(tags=["skills"])

for _platform in ("x", "li", "ig", "yt", "fb"):
    def _make_redirect(ref_val: str):
        @utm_router.get(f"/{ref_val}/{{skill_slug}}", include_in_schema=False)
        def _platform_redirect(skill_slug: str, ref_val: str = ref_val):
            # marketing_1205: set cookie BEFORE redirect so it lands on the
            # visitor on the same recipes.wisechef.ai origin, then 302 to the
            # public portal skill page (statically served by Caddy from
            # /home/wisechef/recipes-portal/dist/skills/<slug>/index.html).
            resp = _RedirectResponse(
                url=f"/skills/{skill_slug}?ref={ref_val}",
                status_code=302,
            )
            _set_utm_ref_cookie(resp, ref_val)
            return resp
        _platform_redirect.__name__ = f"redirect_{ref_val}_slug"
        return _platform_redirect
    _make_redirect(_platform)


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


# Tier rank lookup — higher rank = more capability. None / unknown = anonymous.
# Phase 5 (RCP-INCIDENT-2026-05-11): canonical slugs are 'pro' and 'pro_plus'.
# Legacy slugs kept for backwards compat until 2026-06-10.
TIER_RANK = {
    None: 0, "free": 0,
    "pro": 1, "cook": 1,           # cook=legacy alias for pro
    "pro_plus": 2, "operator": 2, "studio": 3,  # operator/studio=legacy for pro_plus
}

# WIS-902: Tier-aware install rate limits (installs per day per API key).
# Free/anon: 5, Pro: 100, Pro+: unlimited.
TIER_INSTALL_LIMITS: dict[str | None, int | None] = {
    None: 5,        # anonymous / no API key
    "free": 5,      # free-tier user
    "pro": 100,     # Pro subscriber
    "pro_plus": None,  # unlimited
    # Legacy aliases:
    "cook": 100,    # legacy alias → pro
    "operator": None,  # legacy alias → pro_plus
    "studio": None,    # legacy alias → pro_plus
}


def _resolve_caller_tier(db: Session, request: Request) -> str | None:
    """Return the calling user's active subscription tier, or None if anonymous.

    The access endpoint is in the middleware's PUBLIC_PREFIXES list, so we
    re-implement a lightweight API-key lookup here to optionally hydrate the
    caller's tier without forcing auth.
    """
    from app.config import settings

    key = request.headers.get("x-api-key")
    if not key or not key.startswith("rec_"):
        return None
    # Master key behaves as a Pro+ subscriber for capability checks.
    if key == settings.API_KEY:
        return "pro_plus"
    import hashlib
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    api_key_obj = (
        db.query(APIKey)
        .filter(APIKey.key_hash == key_hash, APIKey.is_active == True)  # noqa: E712
        .first()
    )
    if not api_key_obj:
        return None
    user = db.query(User).filter(User.id == api_key_obj.user_id).first()
    if not user or user.subscription_status not in ("active", "trialing"):
        return None
    return user.subscription_tier


def _resolve_caller_tier_for_install(db: Session, request: Request) -> str | None:
    """Resolve caller tier from request.state (set by APIKeyMiddleware).

    Returns the user's subscription tier, or None for anonymous/master.
    Master key gets unlimited installs (treated as operator tier).
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    # Master key gets unlimited installs (treated as pro_plus tier).
    if api_key_user_id is None:
        return "pro_plus"
    if api_key_user_id == "MISSING" or api_key_user_id == "CBT_TOKEN":
        return None
    
    user = db.query(User).filter(User.id == api_key_user_id).first()
    if not user or user.subscription_status not in ("active", "trialing"):
        return None
    return user.subscription_tier


def _count_today_installs(db: Session, api_key_id) -> int:
    """Count installs today for a given API key ID."""
    if api_key_id is None:
        return 0
    today_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (
        db.query(func.count(InstallEvent.id))
        .filter(
            InstallEvent.api_key_id == api_key_id,
            InstallEvent.created_at >= today_start,
        )
        .scalar() or 0
    )


@router.get("/skills/access", response_model=SkillAccessOut, tags=["skills"])
def skill_access(
    request: Request,
    skill: str = Query(..., description="Skill slug to check access for"),
    fork_eligible: bool = Query(
        False,
        description="If true, require Operator+ tier (fork capability) on top of skill-tier access. Forks API ships in a later batch.",
    ),
    db: Session = Depends(get_db),
):
    """Check whether the calling subscriber can access a skill.

    Tier semantics (Plan v5.4 §A.8):
      - Cook subscribers can access any current skill (all skills are
        currently cook-tier or below).
      - Operator subscribers add fork capability — pass ``fork_eligible=true``
        to gate access on it.
      - Studio subscribers add bucket capability (bucket endpoints land in a
        later batch; ``bucket_eligible`` is reported on every response).
    """
    s = db.query(Skill).filter(Skill.slug == skill).first()
    if not s:
        raise HTTPException(status_code=404, detail=f"Skill '{skill}' not found")

    user_tier = _resolve_caller_tier(db, request)
    user_rank = TIER_RANK.get(user_tier, 0)
    # Skills with no explicit tier default to pro — the marketplace baseline.
    skill_rank = TIER_RANK.get(s.tier, TIER_RANK["pro"])

    has_access = s.is_public and user_rank >= skill_rank
    if fork_eligible:
        has_access = has_access and user_rank >= TIER_RANK["pro_plus"]

    return SkillAccessOut(
        slug=s.slug,
        title=s.title,
        has_access=has_access,
        tier=s.tier,
        user_tier=user_tier,
        fork_eligible=user_rank >= TIER_RANK["operator"],
        bucket_eligible=user_rank >= TIER_RANK["studio"],
        latest_version=s.versions[0].semver if s.versions else None,
        license=s.license,
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
        .filter(Skill.is_public == True)  # noqa: E712
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
        db.query(SkillDerivedEdge.source_slug,
                 SkillDerivedEdge.target_slug,
                 SkillDerivedEdge.weight)
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
def get_skill_detail(slug: str, db: Session = Depends(get_db)):
    """Full skill detail with versions and resolved related skills."""
    skill = (
        db.query(Skill)
        .options(joinedload(Skill.versions), joinedload(Skill.creator))
        .filter(Skill.slug == slug, Skill.is_public == True)
        .first()
    )
    if not skill:
        # Phase J — check skill_aliases for a non-expired redirect.
        alias = (
            db.query(SkillAlias)
            .filter(SkillAlias.old_slug == slug)
            .one_or_none()
        )
        if alias is not None:
            now = datetime.now(timezone.utc)
            expires = alias.expires_at
            # SQLite returns naive datetimes; treat naive as UTC for comparison.
            if expires is not None and expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
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
        install_count_total=total_count,
        install_count_7d=last_7d,
        readme=skill.readme,
        license=skill.license,
        # v6 Phase A catalog fields
        skill_variant=getattr(skill, "skill_variant", "custom") or "custom",
        original_source_url=getattr(skill, "original_source_url", None),
        parent_skill_slug=getattr(skill, "parent_skill_slug", None),
        pinned_sha=getattr(skill, "pinned_sha", None),
        upstream_status=getattr(skill, "upstream_status", "active") or "active",
        external_resources=getattr(skill, "external_resources", None),
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


@router.get("/skills/{slug}/external", tags=["skills"])
def get_skill_external(slug: str, db: Session = Depends(get_db)):
    """v6 Phase A: Return external_resources JSON for a skill.

    Public, no auth — surfaces the "you might also want" upstream links the
    skill author declared in frontmatter. Empty list if none, 404 if skill missing.
    """
    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    resources = getattr(skill, "external_resources", None) or []
    if not isinstance(resources, list):
        resources = []
    return resources


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

    # RCP-13: keep the denormalised Skill.install_count counter in sync with
    # telemetry. The trending endpoint and live API responses compute counts
    # directly from telemetry/install_events, but Skill.install_count is the
    # popularity scoring input used by the carousel selector and by any future
    # ORDER BY popularity queries. Increment atomically (SQL-level expression,
    # not a Python read-modify-write) so concurrent installs cannot lose
    # writes. Same DB transaction as the telemetry insert — either both land
    # or neither does.
    if body.event_type == "install" and skill_id is not None:
        db.query(Skill).filter(Skill.id == skill_id).update(
            {Skill.install_count: Skill.install_count + 1},
            synchronize_session=False,
        )

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
