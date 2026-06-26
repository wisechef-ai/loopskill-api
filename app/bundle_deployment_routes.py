"""Bundle deployment API — Pro+ white-label + ordered-apply surface.

spotify_0608 Ph A — re-homed from the retired ``buckets_routes`` (D1). Bundle
is the survivor primitive; this module is the *deployment* layer (ordered apply,
forks, white-label custom domains, preflight, public manifest). It operates on
``Bundle`` rows and the new ``CookbookDeployment`` table — the lossless
replacement for ``BucketSkill``. The membership layer (``CookbookSkill`` + the
existing ``/api/cookbooks`` CRUD in ``bundle_routes``) is untouched.  # compat-alias

Mounted under ``/api/cookbook-deploy`` to avoid any path collision with the
existing ``/api/cookbooks`` CRUD surface.

Tier gate: every endpoint except ``GET /api/cookbook-deploy/{slug}/manifest``
and ``POST /api/cookbook-deploy/{slug}/preflight`` requires the authenticated
user to be on the ``pro`` (or above) subscription tier. The manifest endpoint
is intentionally public so it can be embedded by white-label sites and shared
between agents.

Endpoints:
  POST   /api/cookbook-deploy/create
  GET    /api/cookbook-deploy/list
  POST   /api/cookbook-deploy/{cookbook_id}/skills/add
  DELETE /api/cookbook-deploy/{cookbook_id}/skills/{skill_id}
  POST   /api/cookbook-deploy/{cookbook_id}/apply
  GET    /api/cookbook-deploy/{cookbook_id}/jobs/{job_id}
  POST   /api/cookbook-deploy/{slug}/preflight
  GET    /api/cookbook-deploy/{slug}/manifest
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth_routes import get_current_user_optional
from app.bundle_preflight import run_preflight
from app.database import get_db
from app.models import Bundle, BundleDeployment, InstallEvent, Skill, User

logger = logging.getLogger(__name__)
_h = APIRouter(tags=["bundle-deploy"])  # Phase 3+4: handlers prefix-free; combined router below


# pro/pro_plus + legacy aliases (cook/studio sunset 2026-06-10)
DEPLOY_TIERS = {"pro", "pro_plus", "studio", "master", "cook"}  # fmt: skip
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


# ── Tier gate ───────────────────────────────────────────────────────────


def _require_deploy_tier(user: User | None) -> User:
    """Enforce pro/pro_plus tier; 401 if anonymous, 402 otherwise."""
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    tier = (user.subscription_tier or "").lower()
    if tier not in DEPLOY_TIERS:
        raise HTTPException(
            status_code=402,
            detail=f"pro_tier_required:current={tier or 'none'}",
        )
    return user


# ── Pydantic models ─────────────────────────────────────────────────────


class DeployCookbookCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    visibility: str = Field(default="private")
    pin_mode: str = Field(default="latest-stable")


class DeploymentAddRequest(BaseModel):
    skill_id: str | None = None
    fork_id: str | None = None
    version_pin: str | None = None
    install_order: int = 100


# ── Helpers ─────────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = s[:63] or f"cookbook-{uuid.uuid4().hex[:8]}"
    if not SLUG_RE.match(s):
        s = f"cookbook-{uuid.uuid4().hex[:8]}"
    return s


def _cookbook_dict(cb: Bundle) -> dict:
    return {
        "id": str(cb.id),
        "owner_id": str(cb.bundle_owner) if cb.bundle_owner else None,
        "name": cb.name,
        "slug": cb.slug,
        "description": cb.description,
        "visibility": cb.visibility,
        "is_white_label": bool(cb.is_white_label),
        "custom_domain": cb.custom_domain,
        "pin_mode": cb.pin_mode,
        "theme_json": cb.theme_json,
        "created_at": cb.created_at.isoformat() if cb.created_at else None,
    }


def _resolve_cookbook_or_404(db: Session, cookbook_id: str, user: User) -> Bundle:
    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_cookbook_id")
    cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if not cb:
        raise HTTPException(status_code=404, detail="cookbook_not_found")
    if cb.bundle_owner != user.id:
        raise HTTPException(status_code=403, detail="forbidden")
    return cb


# ── Endpoints ───────────────────────────────────────────────────────────


@_h.post("/create")
async def create_deploy_cookbook(
    req: DeployCookbookCreateRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Create a new white-label deployment cookbook for the authenticated Pro user."""
    user = _require_deploy_tier(user)
    if req.visibility not in {"private", "team", "public"}:
        raise HTTPException(status_code=400, detail="invalid_visibility")
    if req.pin_mode not in {"latest-stable", "pinned-current", "frozen"}:
        raise HTTPException(status_code=400, detail="invalid_pin_mode")

    base_slug = _slugify(req.name)
    slug = base_slug
    suffix = 0
    while db.query(Bundle).filter(Bundle.slug == slug).first() is not None:
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    cb = Bundle(
        id=uuid.uuid4(),
        bundle_owner=user.id,
        name=req.name,
        slug=slug,
        description=req.description,
        visibility=req.visibility,
        pin_mode=req.pin_mode,
    )
    db.add(cb)
    db.commit()
    db.refresh(cb)
    logger.info("deploy_cookbook_created id=%s slug=%s owner=%s", cb.id, cb.slug, user.id)
    return {"status": "created", "cookbook": _cookbook_dict(cb)}


@_h.get("/list")
async def list_deploy_cookbooks(
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """List all deployment cookbooks owned by the authenticated Pro user."""
    user = _require_deploy_tier(user)
    rows = (
        db.query(Bundle)
        .filter(Bundle.bundle_owner == user.id, Bundle.slug.isnot(None))  # compat-alias
        .order_by(Bundle.created_at.desc())
        .all()
    )
    return {"cookbooks": [_cookbook_dict(c) for c in rows]}


@_h.post("/{cookbook_id}/skills/add")  # compat-alias
async def add_deployment(
    cookbook_id: str = Path(...),
    req: DeploymentAddRequest = Body(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Add a skill or fork as an ordered deployment in the specified cookbook."""
    user = _require_deploy_tier(user)
    cb = _resolve_cookbook_or_404(db, cookbook_id, user)

    if not req.skill_id and not req.fork_id:
        raise HTTPException(status_code=400, detail="skill_id_or_fork_id_required")
    if req.skill_id and req.fork_id:
        raise HTTPException(status_code=400, detail="skill_id_xor_fork_id")

    skill_uuid: UUID | None = None
    fork_uuid: UUID | None = None
    if req.skill_id:
        try:
            skill_uuid = UUID(req.skill_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid_skill_id")
        if not db.query(Skill).filter(Skill.id == skill_uuid).first():
            raise HTTPException(status_code=404, detail="skill_not_found")
    if req.fork_id:
        try:
            fork_uuid = UUID(req.fork_id)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid_fork_id")
        # Forks live on the sibling branch — we accept the UUID without
        # FK-checking it here. The DB-level FK (when both branches merge)
        # provides ultimate integrity.

    row = BundleDeployment(
        id=uuid.uuid4(),
        bundle_id=cb.id,
        skill_id=skill_uuid,
        fork_id=fork_uuid,
        version_pin=req.version_pin,
        install_order=req.install_order,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "status": "added",
        "id": str(row.id),
        "cookbook_id": str(cb.id),
        "skill_id": str(skill_uuid) if skill_uuid else None,
        "fork_id": str(fork_uuid) if fork_uuid else None,
        "version_pin": row.version_pin,
        "install_order": row.install_order,
    }


@_h.delete("/{cookbook_id}/skills/{skill_id}")  # compat-alias
async def remove_deployment(
    cookbook_id: str,
    skill_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Remove a skill/fork deployment from the specified cookbook."""
    user = _require_deploy_tier(user)
    cb = _resolve_cookbook_or_404(db, cookbook_id, user)
    try:
        skill_uuid = UUID(skill_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_skill_id")

    rows = (
        db.query(BundleDeployment)
        .filter(
            BundleDeployment.bundle_id == cb.id,  # compat-alias
            (BundleDeployment.skill_id == skill_uuid) | (BundleDeployment.fork_id == skill_uuid),
        )
        .all()
    )
    for r in rows:
        db.delete(r)
    db.commit()
    return {"status": "removed", "count": len(rows)}


@_h.post("/{cookbook_id}/apply")  # compat-alias
async def apply_cookbook(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Kick off an atomic install across all cookbook deployments (ordered).

    Returns a synthesized job_id; an InstallEvent is written for each skill in
    the cookbook with the cookbook annotation carried in `client_ip` (legacy
    column) so dashboards can join apply-events back to the originating
    cookbook. The actual install work runs agent-side via the meta-skill
    `recipes apply` command; this endpoint records intent + returns the manifest.
    """
    user = _require_deploy_tier(user)
    cb = _resolve_cookbook_or_404(db, cookbook_id, user)
    rows = (
        db.query(BundleDeployment)
        .filter(BundleDeployment.bundle_id == cb.id)  # compat-alias
        .order_by(BundleDeployment.install_order.asc())
        .all()
    )

    job_id = uuid.uuid4()
    cookbook_annotation = f"cookbook:{cb.id}"
    for row in rows:
        if not row.skill_id:
            continue
        skill = db.query(Skill).filter(Skill.id == row.skill_id).first()
        if not skill:
            continue
        ev = InstallEvent(
            id=uuid.uuid4(),
            skill_id=skill.id,
            skill_slug=skill.slug,
            version_semver=row.version_pin or "latest",
            client_ip=cookbook_annotation,
            created_at=datetime.now(UTC),
        )
        db.add(ev)
    db.commit()

    logger.info(
        "cookbook_apply cookbook=%s job=%s skills=%d owner=%s",
        cb.id,
        job_id,
        len(rows),
        user.id,
    )
    return {
        "status": "applying",
        "job_id": str(job_id),
        "cookbook_id": str(cb.id),
        "skills": len([r for r in rows if r.skill_id]),
    }


@_h.get("/{cookbook_id}/jobs/{job_id}")  # compat-alias
async def cookbook_job_status(
    cookbook_id: str,
    job_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Poll status for a cookbook-apply job.

    Thin wrapper that always returns 'applying' — a real terminal state lands
    when the health-check-before-ready work ships. Returning a stable shape now
    means the dashboard apply-panel can be wired without a follow-up rev.
    """
    user = _require_deploy_tier(user)
    _ = _resolve_cookbook_or_404(db, cookbook_id, user)
    return {"job_id": job_id, "status": "applying"}


@_h.post("/{slug}/preflight")
async def bundle_preflight(
    slug: str,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
):
    """Pre-flight green-light check for `recipes apply cookbook://<slug>`.

    Public so the meta-skill can call it without an OAuth round-trip; the body
    carries the host fingerprint that drives compat checks. The endpoint only
    reads — no side effects — so making it unauthenticated is safe.
    """
    host_fp = body.get("host_fingerprint") if isinstance(body, dict) else None
    host_ports = body.get("host_ports_in_use") if isinstance(body, dict) else None
    host_env = body.get("host_env") if isinstance(body, dict) else None
    return run_preflight(
        db=db,
        cookbook_slug=slug,
        host_fingerprint=host_fp,
        host_ports_in_use=host_ports,
        host_env=host_env,
    )


@_h.get("/{slug}/manifest")
async def cookbook_deploy_manifest(
    slug: str,
    db: Session = Depends(get_db),
):
    """Public manifest — no auth, no secrets.

    Returns the cookbook plus the ordered list of deployments. Forks are
    emitted by id only since the source tarballs require an authenticated
    install against `/api/forks/{id}/install`.
    """
    cb = db.query(Bundle).filter(Bundle.slug == slug).first()
    if not cb:
        raise HTTPException(status_code=404, detail="cookbook_not_found")
    if cb.visibility == "private":
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    rows = (
        db.query(BundleDeployment)
        .filter(BundleDeployment.bundle_id == cb.id)  # compat-alias
        .order_by(BundleDeployment.install_order.asc())
        .all()
    )
    skills = []
    for row in rows:
        entry = {
            "version_pin": row.version_pin,
            "install_order": row.install_order,
        }
        if row.skill_id:
            skill = db.query(Skill).filter(Skill.id == row.skill_id).first()
            if skill:
                entry["skill"] = {"id": str(skill.id), "slug": skill.slug, "title": skill.title}
        if row.fork_id:
            entry["fork"] = {"id": str(row.fork_id)}
        skills.append(entry)

    return {
        "cookbook": {
            "name": cb.name,
            "slug": cb.slug,
            "description": cb.description,
            "visibility": cb.visibility,
            "pin_mode": cb.pin_mode,
            "is_white_label": bool(cb.is_white_label),
            "theme": cb.theme_json,
        },
        "skills": skills,
    }


# ── Phase 3+4: combined router with new canonical prefix + compat alias ──────
# /api/bundle-deploy is the new primary vocabulary; /api/cookbook-deploy stays as  # compat-alias
# a compat-alias so existing callers (JWT-authed portal, test suite) still work.
router = APIRouter()
router.include_router(_h, prefix="/api/bundle-deploy", tags=["bundle-deploy"])
router.include_router(_h, prefix="/api/cookbook-deploy", tags=["cookbook-deploy"])  # compat-alias
