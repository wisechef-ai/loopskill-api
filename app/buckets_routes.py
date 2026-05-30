"""Buckets API — Pro-tier collections of skills/forks (Phase E.2, v5.4).

Tier gate: every endpoint except `GET /api/buckets/{slug}/manifest` requires
the authenticated user to be on the `pro` (or above) subscription tier.
The manifest endpoint is intentionally public so it can be embedded by
white-label sites and shared between agents.

integrator_2905 W1: gate dropped from pro_plus to pro for broader first-dollar
funnel. Legacy aliases still accepted until 2026-06-10.

Endpoints:
  POST   /api/buckets/create
  GET    /api/buckets/list
  POST   /api/buckets/{id}/skills/add
  DELETE /api/buckets/{id}/skills/{skill_id}
  POST   /api/buckets/{id}/apply
  GET    /api/buckets/{slug}/manifest
  GET    /api/buckets/{id}/jobs/{job_id}
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
from app.bucket_preflight import run_preflight
from app.database import get_db
from app.models import Bucket, BucketSkill, InstallEvent, Skill, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/buckets", tags=["buckets"])


STUDIO_TIERS = {"pro", "pro_plus", "studio", "master", "cook"}  # studio/cook = legacy aliases (sunset 2026-06-10)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


# ── Tier gate ───────────────────────────────────────────────────────────


def _require_studio(user: User | None) -> User:
    """Enforce pro_plus/master tier; 401 if anonymous, 402 otherwise."""
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    tier = (user.subscription_tier or "").lower()
    if tier not in STUDIO_TIERS:
        raise HTTPException(
            status_code=402,
            detail=f"studio_tier_required:current={tier or 'none'}",
        )
    return user


# ── Pydantic models ─────────────────────────────────────────────────────


class BucketCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    visibility: str = Field(default="private")
    pin_mode: str = Field(default="latest-stable")


class BucketSkillAddRequest(BaseModel):
    skill_id: str | None = None
    fork_id: str | None = None
    version_pin: str | None = None
    install_order: int = 100


# ── Helpers ─────────────────────────────────────────────────────────────


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    s = s[:63] or f"bucket-{uuid.uuid4().hex[:8]}"
    if not SLUG_RE.match(s):
        # fall back if slugified form isn't valid
        s = f"bucket-{uuid.uuid4().hex[:8]}"
    return s


def _bucket_dict(bucket: Bucket) -> dict:
    return {
        "id": str(bucket.id),
        "owner_id": str(bucket.owner_id),
        "name": bucket.name,
        "slug": bucket.slug,
        "description": bucket.description,
        "visibility": bucket.visibility,
        "is_white_label": bool(bucket.is_white_label),
        "custom_domain": bucket.custom_domain,
        "pin_mode": bucket.pin_mode,
        "theme_json": bucket.theme_json,
        "created_at": bucket.created_at.isoformat() if bucket.created_at else None,
    }


def _resolve_bucket_or_404(db: Session, bucket_id: str, user: User) -> Bucket:
    try:
        bucket_uuid = UUID(bucket_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_bucket_id")
    bucket = db.query(Bucket).filter(Bucket.id == bucket_uuid).first()
    if not bucket:
        raise HTTPException(status_code=404, detail="bucket_not_found")
    if bucket.owner_id != user.id:
        raise HTTPException(status_code=403, detail="forbidden")
    return bucket


# ── Endpoints ───────────────────────────────────────────────────────────


@router.post("/create")
async def create_bucket(
    req: BucketCreateRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Create a new skill bucket for the authenticated pro_plus user."""
    user = _require_studio(user)
    if req.visibility not in {"private", "team", "public"}:
        raise HTTPException(status_code=400, detail="invalid_visibility")
    if req.pin_mode not in {"latest-stable", "pinned-current", "frozen"}:
        raise HTTPException(status_code=400, detail="invalid_pin_mode")

    base_slug = _slugify(req.name)
    slug = base_slug
    suffix = 0
    while db.query(Bucket).filter(Bucket.slug == slug).first() is not None:
        suffix += 1
        slug = f"{base_slug}-{suffix}"

    bucket = Bucket(
        id=uuid.uuid4(),
        owner_id=user.id,
        name=req.name,
        slug=slug,
        description=req.description,
        visibility=req.visibility,
        pin_mode=req.pin_mode,
    )
    db.add(bucket)
    db.commit()
    db.refresh(bucket)
    logger.info("bucket_created id=%s slug=%s owner=%s", bucket.id, bucket.slug, user.id)
    return {"status": "created", "bucket": _bucket_dict(bucket)}


@router.get("/list")
async def list_buckets(
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """List all skill buckets owned by the authenticated pro_plus user."""
    user = _require_studio(user)
    buckets = db.query(Bucket).filter(Bucket.owner_id == user.id).order_by(Bucket.created_at.desc()).all()
    return {"buckets": [_bucket_dict(b) for b in buckets]}


@router.post("/{bucket_id}/skills/add")
async def add_skill_to_bucket(
    bucket_id: str = Path(...),
    req: BucketSkillAddRequest = Body(...),
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Add a skill to the specified bucket."""
    user = _require_studio(user)
    bucket = _resolve_bucket_or_404(db, bucket_id, user)

    if not req.skill_id and not req.fork_id:
        raise HTTPException(status_code=400, detail="skill_id_or_fork_id_required")

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

    row = BucketSkill(
        id=uuid.uuid4(),
        bucket_id=bucket.id,
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
        "bucket_id": str(bucket.id),
        "skill_id": str(skill_uuid) if skill_uuid else None,
        "fork_id": str(fork_uuid) if fork_uuid else None,
        "version_pin": row.version_pin,
        "install_order": row.install_order,
    }


@router.delete("/{bucket_id}/skills/{skill_id}")
async def remove_skill_from_bucket(
    bucket_id: str,
    skill_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Remove a skill from the specified bucket."""
    user = _require_studio(user)
    bucket = _resolve_bucket_or_404(db, bucket_id, user)
    try:
        skill_uuid = UUID(skill_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_skill_id")

    rows = (
        db.query(BucketSkill)
        .filter(
            BucketSkill.bucket_id == bucket.id,
            (BucketSkill.skill_id == skill_uuid) | (BucketSkill.fork_id == skill_uuid),
        )
        .all()
    )
    for r in rows:
        db.delete(r)
    db.commit()
    return {"status": "removed", "count": len(rows)}


@router.post("/{bucket_id}/apply")
async def apply_bucket(
    bucket_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Kick off an atomic install across all bucket skills.

    Returns a synthesized job_id; an InstallEvent is written for each skill
    in the bucket with `bucket_id` carried in `client_ip` (legacy column —
    we store the bucket UUID as the annotation source) so dashboards can
    join apply-events back to the originating bucket. The actual install
    work runs on the agent side via the meta-skill `recipes apply` command;
    this endpoint just records intent and returns the manifest.
    """
    user = _require_studio(user)
    bucket = _resolve_bucket_or_404(db, bucket_id, user)
    skills = (
        db.query(BucketSkill)
        .filter(BucketSkill.bucket_id == bucket.id)
        .order_by(BucketSkill.install_order.asc())
        .all()
    )

    job_id = uuid.uuid4()
    bucket_annotation = f"bucket:{bucket.id}"
    for row in skills:
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
            # Re-use client_ip column to carry the bucket annotation. The
            # canonical "applying" status is also recorded here so that
            # downstream queries can filter by status='applying' once the
            # install_events.status column lands in F.6.
            client_ip=bucket_annotation,
            created_at=datetime.now(UTC),
        )
        db.add(ev)
    db.commit()

    logger.info(
        "bucket_apply bucket=%s job=%s skills=%d owner=%s",
        bucket.id,
        job_id,
        len(skills),
        user.id,
    )
    return {
        "status": "applying",
        "job_id": str(job_id),
        "bucket_id": str(bucket.id),
        "skills": len(skills),
    }


@router.get("/{bucket_id}/jobs/{job_id}")
async def bucket_job_status(
    bucket_id: str,
    job_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Poll status for a bucket-apply job.

    For Phase E this is a thin wrapper that always returns 'applying' —
    Phase F.5 (health-check before "ready") will populate a real terminal
    state. Returning a stable shape now means the dashboard apply-panel can
    be wired without a follow-up rev.
    """
    user = _require_studio(user)
    _ = _resolve_bucket_or_404(db, bucket_id, user)
    return {"job_id": job_id, "status": "applying"}


@router.post("/{slug}/preflight")
async def bucket_preflight(
    slug: str,
    body: dict = Body(default={}),
    db: Session = Depends(get_db),
):
    """Pre-flight green-light check for `recipes apply bucket://<slug>`.

    Public so the meta-skill can call it without an OAuth round-trip; the
    body carries the host fingerprint that drives compat checks. The endpoint
    only reads — no side effects — so making it unauthenticated is safe.
    """
    host_fp = body.get("host_fingerprint") if isinstance(body, dict) else None
    host_ports = body.get("host_ports_in_use") if isinstance(body, dict) else None
    host_env = body.get("host_env") if isinstance(body, dict) else None
    return run_preflight(
        db=db,
        bucket_slug=slug,
        host_fingerprint=host_fp,
        host_ports_in_use=host_ports,
        host_env=host_env,
    )


@router.get("/{slug}/manifest")
async def bucket_manifest(
    slug: str,
    db: Session = Depends(get_db),
):
    """Public manifest — no auth, no secrets.

    Returns the bucket plus the ordered list of skills. Forks are emitted
    by id only since the source tarballs require an authenticated install
    against `/api/forks/{id}/install`.
    """
    bucket = db.query(Bucket).filter(Bucket.slug == slug).first()
    if not bucket:
        raise HTTPException(status_code=404, detail="bucket_not_found")
    if bucket.visibility == "private":
        raise HTTPException(status_code=404, detail="bucket_not_found")

    rows = (
        db.query(BucketSkill)
        .filter(BucketSkill.bucket_id == bucket.id)
        .order_by(BucketSkill.install_order.asc())
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
        "bucket": {
            "name": bucket.name,
            "slug": bucket.slug,
            "description": bucket.description,
            "visibility": bucket.visibility,
            "pin_mode": bucket.pin_mode,
            "is_white_label": bool(bucket.is_white_label),
            "theme": bucket.theme_json,
        },
        "skills": skills,
    }
