"""Cookbook CRUD endpoints — v7 Phase B.

Endpoints (all gated to subscription_tier in {'pro','pro_plus'} OR master key):
Legacy slugs 'cook'/'operator' accepted via _is_paid_tier/_is_pro_plus_tier shims for 30 days.
  - POST   /api/cookbooks                       create (1-max for cook tier)
  - GET    /api/cookbooks                       list mine
  - GET    /api/cookbooks/{id}                  detail with skills
  - POST   /api/cookbooks/{id}/skills           add skill (validates slug)
  - DELETE /api/cookbooks/{id}/skills/{slug}    soft-delete (source='disabled')
  - POST   /api/cookbooks/{id}/install          idempotent install payload
  - GET    /api/cookbooks/{id}/manifest         YAML manifest
  - GET    /api/cookbooks/{id}/sync             since-filter event log

Tier gate: middleware stamps api_key_user_id on request.state. The static master
key bypasses tier checks. Free / no-tier users receive 401 on create. Pro tier
is capped at 1 cookbook (403 on second). Pro+ is unlimited.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Cookbook, CookbookSkill, Skill, SkillVersion, User
from app.tier_labels import _is_paid_tier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cookbooks", tags=["cookbooks"])

# RCP-INCIDENT-2026-05-11: COOKBOOK_TIERS and UNLIMITED_TIERS now use helper
# functions (_is_paid_tier, _is_operator_tier) defined in tier_labels.py which
# transparently accept the legacy 'studio' slug for 30 days. These set
# constants remain for reference/documentation only — do not use for gate checks.
COOKBOOK_TIERS = {"pro", "pro_plus"}  # canonical; legacy slugs handled via shim
UNLIMITED_TIERS = {"pro_plus"}  # canonical; legacy slugs handled via shim
ACTIVE_SUB_STATUSES = {"active", "trialing"}
ALLOWED_SOURCES = {"forked", "custom-added", "overridden", "disabled"}

# WIS-902: Cook tier skill cap per cookbook
COOK_SKILL_CAP = 25


# ── CBT scope enforcement for cookbook routes ─────────────────────────────


def _enforce_cbt_scope_for_cookbook_route(request: Request, cookbook_id: str) -> None:
    """Enforce cbt_ token scope for cookbook-level routes.

    Raises 403 if:
      - cbt_ token's cookbook_id != route's cookbook_id
      - cbt_ token scope is 'read' and method is not GET
    No-op if no cbt_ token is present (rec_ key path).

    cookbook_share_2105 Phase D — vocabulary expanded:
      scope ∈ {read, edit, install}
      read    → GET only
      edit    → all cookbook operations (current behaviour)
      install → GET + POST /install (read + bulk install). Cannot add/remove
                skills, cannot create child tokens — narrow on purpose. Used
                by "share my cookbook with another agent" flows so the
                recipient can install but not modify.
    """
    scope = getattr(request.state, "cookbook_token_scope", None)
    if scope is None:
        return  # No cbt_ token; rec_ key path

    token_cb_id = getattr(request.state, "cookbook_token_cookbook_id", None)
    try:
        cid = UUID(cookbook_id)
    except (ValueError, TypeError):
        return  # Let downstream handle invalid ID

    if token_cb_id != cid:
        raise HTTPException(
            status_code=403,
            detail="Token scope mismatch (wrong cookbook)",
        )

    if scope == "read" and request.method != "GET":
        # cookbook_share_2105 Phase D: clearer scope-insufficient message.
        # Kept as a plain string (not a dict) so existing clients that read
        # ``resp.json()["detail"]`` as text continue to work — see
        # test_cbt_read_token_blocks_skill_add. The "SCOPE_INSUFFICIENT"
        # token is included in-line so programmatic callers can grep for it.
        raise HTTPException(
            status_code=403,
            detail="SCOPE_INSUFFICIENT: token scope 'read' insufficient; need 'install' or higher",
        )

    if scope == "install":
        # install scope: GET + POST /install only. Block any other mutation.
        path = request.url.path
        is_install_route = path.endswith("/install") or "/install" in path
        if request.method != "GET" and not is_install_route:
            raise HTTPException(
                status_code=403,
                detail=(
                    "SCOPE_INSUFFICIENT: token scope 'install' permits GET + /install only; "
                    "need 'edit' for cookbook modification"
                ),
            )

    # SECURITY: cbt_ tokens NEVER authorize publishing, regardless of scope.
    # Even if a /api/cookbooks/{id}/_publish route is added in the future,
    # this gate blocks it. Same for any path containing /_publish.
    if "/_publish" in request.url.path:
        raise HTTPException(
            status_code=403,
            detail="Share tokens cannot authorize publishing",
        )


# ── Tier gate ────────────────────────────────────────────────────────────


class CookbookCtx(BaseModel):
    user_id: UUID | None = None
    is_master: bool = False
    tier: str | None = None
    # SECURITY: when populated, this caller authenticated via a cbt_ share token
    # scoped to this single cookbook. Route-level checks must enforce that any
    # cb the request acts on equals this value, and must block writes if scope='read'.
    cbt_cookbook_id: UUID | None = None

    model_config = {"arbitrary_types_allowed": True}


def require_cookbook_tier(request: Request, db: Session = Depends(get_db)) -> CookbookCtx:
    """401 unless caller has an active pro/pro_plus sub OR is master.

    SECURITY: cbt_ share tokens stamp api_key_user_id="CBT_TOKEN" (sentinel)
    rather than None — None is the master-key signal. Without this guard
    a cbt_ token would inherit master-tier access. Cbt_ tokens fall through
    to the route-level scope checks in app/share_token_routes.py.
    """
    is_cbt = getattr(request.state, "is_cbt_token", False)
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")

    # cbt_ token: no user, not master. The route-level scope checks gate access.
    if is_cbt or api_key_user_id == "CBT_TOKEN":
        cookbook_id = getattr(request.state, "cookbook_token_cookbook_id", None)
        return CookbookCtx(user_id=None, is_master=False, tier="pro", cbt_cookbook_id=cookbook_id)

    if api_key_user_id is None:
        return CookbookCtx(user_id=None, is_master=True, tier="pro_plus")

    if api_key_user_id == "MISSING":
        raise HTTPException(status_code=401, detail="auth_required")

    user = db.query(User).filter(User.id == api_key_user_id).first()
    tier = user.subscription_tier if user else None
    status = user.subscription_status if user else None

    if not _is_paid_tier(tier) or status not in ACTIVE_SUB_STATUSES:
        raise HTTPException(
            status_code=401,
            detail={"needs_tier": "pro", "current_tier": tier},
        )
    return CookbookCtx(user_id=user.id, is_master=False, tier=tier)


# ── Schemas ──────────────────────────────────────────────────────────────


class CookbookCreateIn(BaseModel):
    name: str
    description: str | None = None


class SkillAddIn(BaseModel):
    slug: str
    source: str | None = "custom-added"


class CookbookSkillOut(BaseModel):
    slug: str
    source: str
    pinned_version: str | None = None
    added_at: datetime | None = None


class CookbookOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    is_base: bool
    parent_cookbook_id: str | None = None
    cookbook_owner: str | None = None
    created_at: datetime | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _resolve_owned_cookbook(db: Session, ctx: CookbookCtx, cookbook_id: str) -> Cookbook:
    try:
        cid = UUID(cookbook_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    cb = db.query(Cookbook).filter(Cookbook.id == cid).first()
    if cb is None:
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    # cookbook_share_2105 Phase D: cbt_token callers (share-token holders) own
    # the resolution path via cookbook_scope match. _enforce_cbt_scope_for_cookbook_route
    # already enforced that ctx.cbt_cookbook_id == cid, so reaching here is
    # authorisation enough.
    if ctx.cbt_cookbook_id is not None and ctx.cbt_cookbook_id == cb.id:
        return cb

    if not ctx.is_master and cb.cookbook_owner != ctx.user_id:
        raise HTTPException(status_code=404, detail="cookbook_not_found")
    return cb


def _skills_for(
    db: Session, cookbook_id: UUID, include_disabled: bool = True
) -> list[tuple[CookbookSkill, Skill]]:
    q = (
        db.query(CookbookSkill, Skill)
        .join(Skill, Skill.id == CookbookSkill.skill_id)
        .filter(CookbookSkill.cookbook_id == cookbook_id)
    )
    if not include_disabled:
        q = q.filter(CookbookSkill.source != "disabled")
    return q.all()


def _to_cb_out(cb: Cookbook) -> dict:
    return CookbookOut(
        id=str(cb.id),
        name=cb.name,
        description=cb.description,
        is_base=bool(cb.is_base),
        parent_cookbook_id=str(cb.parent_cookbook_id) if cb.parent_cookbook_id else None,
        cookbook_owner=str(cb.cookbook_owner) if cb.cookbook_owner else None,
        created_at=cb.created_at,
    ).model_dump(mode="json")


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("", status_code=201)
def create_cookbook(
    body: CookbookCreateIn,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Create a new cookbook for the authenticated user."""
    if ctx.is_master:
        raise HTTPException(status_code=400, detail="master key cannot create user-owned cookbooks")

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="invalid_name")

    if ctx.tier == "pro" or ctx.tier == "cook":  # cook=legacy alias, remove after 2026-06-10
        existing = db.query(Cookbook).filter(Cookbook.cookbook_owner == ctx.user_id).count()
        if existing >= 1:
            raise HTTPException(
                status_code=403,
                detail={"reason": "pro_tier_limit", "max_cookbooks": 1},
            )

    cb = Cookbook(
        id=uuid4(),
        name=name,
        description=body.description,
        is_base=False,
        cookbook_owner=ctx.user_id,
    )
    db.add(cb)
    db.commit()
    db.refresh(cb)
    return _to_cb_out(cb)


@router.get("")
def list_cookbooks(
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """List all cookbooks for the authenticated user."""
    if ctx.is_master:
        return {"cookbooks": []}

    rows = (
        db.query(Cookbook)
        .filter(Cookbook.cookbook_owner == ctx.user_id)
        .order_by(Cookbook.created_at.desc())
        .all()
    )
    return {"cookbooks": [_to_cb_out(r) for r in rows]}


@router.get("/{cookbook_id}")
def get_cookbook(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Return a single cookbook by ID, including its skill list."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    rows = _skills_for(db, cb.id, include_disabled=True)
    out = _to_cb_out(cb)
    out["skills"] = [
        CookbookSkillOut(
            slug=skill.slug,
            source=cs.source,
            pinned_version=cs.pinned_version,
            added_at=cs.added_at,
        ).model_dump(mode="json")
        for cs, skill in rows
    ]
    return out


@router.post("/{cookbook_id}/skills", status_code=201)
def add_skill_to_cookbook(
    cookbook_id: str,
    body: SkillAddIn,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Add a skill to the specified cookbook."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    source = body.source or "custom-added"
    if source not in ALLOWED_SOURCES:
        raise HTTPException(status_code=422, detail="invalid_source")

    skill = db.query(Skill).filter(Skill.slug == body.slug).first()
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")

    existing = (
        db.query(CookbookSkill)
        .filter(
            CookbookSkill.cookbook_id == cb.id,
            CookbookSkill.skill_id == skill.id,
        )
        .first()
    )
    if existing is not None:
        existing.source = source
        db.commit()
        return {
            "cookbook_id": str(cb.id),
            "slug": skill.slug,
            "source": existing.source,
            "added_at": existing.added_at.isoformat() if existing.added_at else None,
            "reactivated": True,
        }

    # WIS-902: Pro tier skill cap
    if ctx.tier == "pro" or ctx.tier == "cook":  # cook=legacy alias, remove after 2026-06-10
        active_count = (
            db.query(CookbookSkill)
            .filter(
                CookbookSkill.cookbook_id == cb.id,
                CookbookSkill.source != "disabled",
            )
            .count()
        )
        if active_count >= COOK_SKILL_CAP:
            raise HTTPException(
                status_code=403,
                detail={
                    "reason": "pro_skill_cap",
                    "max_skills": COOK_SKILL_CAP,
                    "current_count": active_count,
                    "upgrade_to": "pro_plus",
                },
            )

    cs = CookbookSkill(
        cookbook_id=cb.id,
        skill_id=skill.id,
        source=source,
    )
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return {
        "cookbook_id": str(cb.id),
        "slug": skill.slug,
        "source": cs.source,
        "added_at": cs.added_at.isoformat() if cs.added_at else None,
        "reactivated": False,
    }


@router.delete("/{cookbook_id}/skills/{slug}")
def remove_skill_from_cookbook(
    cookbook_id: str,
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Remove a skill from the specified cookbook."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")

    cs = (
        db.query(CookbookSkill)
        .filter(
            CookbookSkill.cookbook_id == cb.id,
            CookbookSkill.skill_id == skill.id,
        )
        .first()
    )
    if cs is None:
        raise HTTPException(status_code=404, detail="skill_not_in_cookbook")

    cs.source = "disabled"
    db.commit()
    return {"cookbook_id": str(cb.id), "slug": slug, "source": "disabled", "deleted": True}


def _make_install_url(skill_slug: str, version_id: UUID, version_semver: str) -> str:
    """Build a signed download URL for a skill version (Issue #27).

    Uses the same HMAC-signing flow as routes.py:recipes_install so the URL
    resolves to /api/skills/_download?token=<signed> — a route that exists
    and serves the tarball bytes.

    The old implementation pointed at /api/skills/{id}/versions/{id}/tarball
    which has never existed in this codebase (zero route matches).
    """
    from itsdangerous import URLSafeTimedSerializer

    # Issue #27 (secfix_1905/I-followup): salt MUST match install_routes._download
    # verifier (salt="recipes-skill-install"). Without this match, every cookbook
    # install URL fails with "Invalid download token" — caught by codex re-pass.
    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="recipes-skill-install")
    token = serializer.dumps({"slug": skill_slug, "version_id": str(version_id), "mode": "install"})
    public_origin = (
        getattr(settings, "PUBLIC_ORIGIN", None)
        or os.environ.get("RECIPES_PUBLIC_ORIGIN")
        or "https://recipes.wisechef.ai"
    )
    return public_origin.rstrip("/") + "/api/skills/_download?token=" + token


@router.post("/{cookbook_id}/install")
def install_cookbook(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Idempotent: re-running returns the same payload. Disabled skills are skipped."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    rows = _skills_for(db, cb.id, include_disabled=False)

    skills_payload = []
    installed_skills: list[tuple[Skill, str]] = []
    for cs, skill in rows:
        version = None
        if cs.pinned_version:
            version = (
                db.query(SkillVersion)
                .filter(
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.semver == cs.pinned_version,
                )
                .first()
            )
        if version is None:
            version = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.created_at.desc())
                .first()
            )

        skills_payload.append(
            {
                "slug": skill.slug,
                "version": version.semver if version else None,
                "tarball_url": _make_install_url(skill.slug, version.id, version.semver) if version else None,
                "checksum_sha256": version.checksum_sha256 if version else None,
                "source": cs.source,
            }
        )
        if version is not None:
            installed_skills.append((skill, version.semver))

    # recipes-D: record an InstallEvent + bump Skill.install_count for every
    # skill that returned a real version. Without this, cookbook-share installs
    # (the only path cbt_-token holders can use) are invisible in transparency
    # stats — the Varys end-to-end install on 2026-05-25 was the demonstration.
    from app._skill_helpers import _record_install_event

    for skill, semver in installed_skills:
        _record_install_event(db, skill=skill, version_semver=semver, request=request, source="cookbook")
    if installed_skills:
        db.commit()

    return {
        "cookbook_id": str(cb.id),
        "name": cb.name,
        "skills": skills_payload,
    }


@router.get("/{cookbook_id}/manifest")
def cookbook_manifest(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Return the install manifest for all skills in a cookbook."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    rows = _skills_for(db, cb.id, include_disabled=True)

    manifest = {
        "name": cb.name,
        "description": cb.description,
        "skills": [
            {
                "slug": skill.slug,
                "source": cs.source,
                "pinned_version": cs.pinned_version,
            }
            for cs, skill in rows
        ],
    }
    body = yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False)
    return Response(content=body, media_type="application/x-yaml")


@router.get("/{cookbook_id}/sync")
def cookbook_sync(
    cookbook_id: str,
    request: Request,
    since: str | None = None,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Return skills updated since the given timestamp for sync."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_since")
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)

    q = (
        db.query(CookbookSkill, Skill)
        .join(Skill, Skill.id == CookbookSkill.skill_id)
        .filter(CookbookSkill.cookbook_id == cb.id)
    )
    if since_dt is not None:
        # SQLite stores naive datetimes; compare naively if necessary.
        q = q.filter(CookbookSkill.added_at >= since_dt.replace(tzinfo=None))

    added: list[dict] = []
    removed: list[dict] = []
    updated: list[dict] = []
    for cs, skill in q.all():
        evt = {
            "slug": skill.slug,
            "source": cs.source,
            "pinned_version": cs.pinned_version,
            "added_at": cs.added_at.isoformat() if cs.added_at else None,
        }
        if cs.source == "disabled":
            removed.append(evt)
        elif cs.source == "overridden":
            updated.append(evt)
        else:
            added.append(evt)

    return {
        "cookbook_id": str(cb.id),
        "since": since_dt.isoformat() if since_dt else None,
        "added": added,
        "removed": removed,
        "updated": updated,
    }


# ── cookbook_share_2105 Phase D — single-skill install under cookbook prefix ──


@router.get("/{cookbook_id}/skills/{slug}/install")
def install_single_skill_from_cookbook(
    cookbook_id: str,
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Install ONE skill from a cookbook by slug.

    Mirror of ``GET /api/skills/install`` but scoped under a cookbook so cbt_
    share tokens (which can ONLY access /api/cookbooks/* paths — see
    middleware.py:389) have a documented single-skill install path.

    Behaviour:
    - 200 with {slug, version, tarball_url, checksum_sha256} on success
    - 404 if the skill is not in this cookbook (or doesn't exist)
    - 403 if the cbt_ token's scope is 'read' (install IS a write-flavoured
      action even though it's GET — gated identically to POST /install)

    Token scope rules (enforced in _enforce_cbt_scope_for_cookbook_route):
      read    → 403 SCOPE_INSUFFICIENT
      install → ok
      edit    → ok (superset of install)
      master/user (owner) → ok
    """
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)

    # install is a write-flavoured action on GET — even with read scope this
    # should 403. The scope-gate above passes 'read' through for any GET, so
    # add a dedicated install-route block here.
    if getattr(request.state, "cookbook_token_scope", None) == "read":
        raise HTTPException(
            status_code=403,
            detail="SCOPE_INSUFFICIENT: token scope 'read' insufficient; need 'install' or higher",
        )

    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    # Find the skill globally; then check it's actually in this cookbook
    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")

    cs = (
        db.query(CookbookSkill)
        .filter(
            CookbookSkill.cookbook_id == cb.id,
            CookbookSkill.skill_id == skill.id,
            CookbookSkill.source != "disabled",
        )
        .first()
    )
    if cs is None:
        raise HTTPException(status_code=404, detail="skill_not_in_cookbook")

    # Pick the right version: pinned if set, else latest
    version: SkillVersion | None = None
    if cs.pinned_version:
        version = (
            db.query(SkillVersion)
            .filter(
                SkillVersion.skill_id == skill.id,
                SkillVersion.semver == cs.pinned_version,
            )
            .first()
        )
    if version is None:
        version = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill.id)
            .order_by(SkillVersion.created_at.desc())
            .first()
        )

    if version is None:
        raise HTTPException(status_code=404, detail="no_versions")

    # recipes-D: record install event + bump counter on this path too.
    from app._skill_helpers import _record_install_event

    _record_install_event(db, skill=skill, version_semver=version.semver, request=request, source="cookbook")
    db.commit()

    return {
        "slug": skill.slug,
        "version": version.semver,
        "tarball_url": _make_install_url(skill.slug, version.id, version.semver),
        "checksum_sha256": version.checksum_sha256,
        "source": cs.source,
    }
