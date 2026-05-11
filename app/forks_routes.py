"""Operator-tier skill forks — Phase D.2.

Endpoints (all gated to subscription_tier in {'operator','studio'} OR master key):
  - POST   /api/forks/create
  - GET    /api/forks/list
  - POST   /api/forks/<id>/version          (multipart: tarball + semver + changelog)
  - GET    /api/forks/<id>/install          (returns HMAC-signed URL, 5-min TTL)
  - GET    /api/forks/_download             (public — verifies the signed token)
  - DELETE /api/forks/<id>                  (soft-delete: visibility=NULL, readme cleared)

Tier gate: middleware validates the API key (header) and stamps user_id on
request.state. This module then loads the User and rejects with HTTP 402 if
their subscription tier is below 'operator'. The static master key bypasses
the tier check (admin, used in tests + ops scripts).

Tarball storage: production path is settings.RECIPES_FORKS_DIR (default
/var/lib/recipes-skills/forks); test envs override RECIPES_FORKS_DIR=/tmp/...
The signed install URL embeds the fork_id + version_id; /api/forks/_download
verifies the HMAC and streams the bytes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import APIKey, ForkVersion, Skill, SkillFork, User
from app.tier_labels import _is_operator_tier

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/forks", tags=["forks"])


# ── Constants ────────────────────────────────────────────────────────────

# RCP-INCIDENT-2026-05-11: OPERATOR_TIERS now uses _is_operator_tier() helper
# from tier_labels.py which transparently accepts legacy 'studio' for 30 days.
# This constant is kept for reference only.
OPERATOR_TIERS = {"operator"}  # canonical; 'studio' handled via shim in _is_operator_tier
ACTIVE_SUB_STATUSES = {"active", "trialing"}
ALLOWED_VISIBILITY = {"private", "team", "public"}

MAX_TARBALL_BYTES = 10 * 1024 * 1024  # 10 MB — matches publisher limit
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.-]+)?$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,127}$")

INSTALL_TOKEN_TTL_SECONDS = 300  # 5 min


def _forks_dir() -> Path:
    """Tarball storage root. Test envs set RECIPES_FORKS_DIR=/tmp/...

    Falls back to settings.RECIPES_SKILLS_DIR/forks so a single env var
    suffices when ops doesn't want a separate forks volume.
    """
    raw = (
        os.environ.get("RECIPES_FORKS_DIR")
        or getattr(settings, "RECIPES_FORKS_DIR", None)
        or str(Path(getattr(settings, "RECIPES_SKILLS_DIR", "/var/lib/recipes-skills")) / "forks")
    )
    return Path(raw)


def _slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:64] or "fork"


# ── Tier gate ────────────────────────────────────────────────────────────

class TierContext(BaseModel):
    user_id: Optional[UUID] = None  # None = master key
    is_master: bool = False
    tier: Optional[str] = None

    model_config = {"arbitrary_types_allowed": True}


def require_operator(request: Request, db: Session = Depends(get_db)) -> TierContext:
    """402 unless caller is master-key OR has an active operator sub.

    The middleware has already validated the API key and stamped api_key_user_id
    on request.state. user_id is None for the static master key.
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is None:
        return TierContext(user_id=None, is_master=True, tier="operator")

    if api_key_user_id == "MISSING":
        raise HTTPException(status_code=401, detail="auth_required")

    user = db.query(User).filter(User.id == api_key_user_id).first()
    tier = user.subscription_tier if user else None
    status = user.subscription_status if user else None

    if not _is_operator_tier(tier) or status not in ACTIVE_SUB_STATUSES:
        raise HTTPException(
            status_code=402,
            detail={"needs_tier": "operator", "current_tier": tier},
        )
    return TierContext(user_id=user.id, is_master=False, tier=tier)


# ── Schemas ──────────────────────────────────────────────────────────────

class ForkCreateIn(BaseModel):
    source_slug: str
    name: str
    readme: Optional[str] = None


class ForkVersionOut(BaseModel):
    id: str
    semver: str
    tarball_size_bytes: int
    checksum_sha256: str
    changelog: Optional[str] = None
    created_at: datetime


class ForkOut(BaseModel):
    id: str
    user_id: str
    source_skill_id: str
    source_slug: Optional[str] = None
    name: str
    slug: str
    readme: Optional[str] = None
    visibility: Optional[str] = None
    created_at: datetime
    latest_version_id: Optional[str] = None
    versions: list[ForkVersionOut] = []


def _to_out(fork: SkillFork, source_slug: Optional[str] = None,
            include_versions: bool = True) -> ForkOut:
    versions = []
    if include_versions and fork.versions:
        versions = [
            ForkVersionOut(
                id=str(v.id),
                semver=v.semver,
                tarball_size_bytes=int(v.tarball_size_bytes),
                checksum_sha256=v.checksum_sha256,
                changelog=v.changelog,
                created_at=v.created_at,
            )
            for v in fork.versions
        ]
    return ForkOut(
        id=str(fork.id),
        user_id=str(fork.user_id),
        source_skill_id=str(fork.source_skill_id),
        source_slug=source_slug,
        name=fork.name,
        slug=fork.slug,
        readme=fork.readme,
        visibility=fork.visibility,
        created_at=fork.created_at,
        latest_version_id=str(fork.latest_version_id) if fork.latest_version_id else None,
        versions=versions,
    )


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post("/create", status_code=201)
def create_fork(
    body: ForkCreateIn,
    db: Session = Depends(get_db),
    ctx: TierContext = Depends(require_operator),
):
    if ctx.is_master:
        raise HTTPException(status_code=400, detail="master key cannot create user-owned forks")

    if not NAME_RE.match(body.name or ""):
        raise HTTPException(status_code=422, detail="invalid_name")

    source = db.query(Skill).filter(Skill.slug == body.source_slug).first()
    if not source or not source.is_public:
        raise HTTPException(status_code=404, detail="source_skill_not_found")

    slug = _slugify(body.name)
    if not SLUG_RE.match(slug):
        raise HTTPException(status_code=422, detail="invalid_slug")

    fork = SkillFork(
        id=uuid4(),
        user_id=ctx.user_id,
        source_skill_id=source.id,
        name=body.name.strip(),
        slug=slug,
        readme=body.readme,
        visibility="private",
    )
    db.add(fork)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"fork_exists: you already have a fork named {slug!r}",
        )
    db.refresh(fork)
    return _to_out(fork, source_slug=source.slug, include_versions=False).model_dump(mode="json")


@router.get("/list")
def list_forks(
    db: Session = Depends(get_db),
    ctx: TierContext = Depends(require_operator),
):
    if ctx.is_master:
        # Master key is admin — returning the entire fork table would be a
        # surprise; require a real user_id for this endpoint.
        return {"forks": []}

    rows = (
        db.query(SkillFork)
        .filter(
            SkillFork.user_id == ctx.user_id,
            SkillFork.visibility.isnot(None),  # exclude soft-deleted
        )
        .order_by(SkillFork.created_at.desc())
        .all()
    )
    source_ids = {r.source_skill_id for r in rows}
    sources = (
        db.query(Skill.id, Skill.slug)
        .filter(Skill.id.in_(source_ids))
        .all()
        if source_ids else []
    )
    by_id = {sid: slug for sid, slug in sources}
    return {
        "forks": [
            _to_out(r, source_slug=by_id.get(r.source_skill_id)).model_dump(mode="json")
            for r in rows
        ]
    }


def _resolve_owned_fork(db: Session, ctx: TierContext, fork_id: str) -> SkillFork:
    try:
        fid = UUID(fork_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="fork_not_found")

    fork = db.query(SkillFork).filter(SkillFork.id == fid).first()
    if fork is None:
        raise HTTPException(status_code=404, detail="fork_not_found")
    if not ctx.is_master and fork.user_id != ctx.user_id:
        raise HTTPException(status_code=404, detail="fork_not_found")
    return fork


@router.post("/{fork_id}/version", status_code=201)
async def upload_fork_version(
    fork_id: str,
    request: Request,
    tarball: UploadFile = File(...),
    semver: str = Form(...),
    changelog: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    ctx: TierContext = Depends(require_operator),
):
    fork = _resolve_owned_fork(db, ctx, fork_id)
    if fork.visibility is None:
        raise HTTPException(status_code=404, detail="fork_not_found")

    if not SEMVER_RE.match(semver):
        raise HTTPException(status_code=422, detail="invalid_semver")

    tarball_bytes = await tarball.read()
    size = len(tarball_bytes)
    if size == 0:
        raise HTTPException(status_code=422, detail="empty_tarball")
    if size > MAX_TARBALL_BYTES:
        raise HTTPException(status_code=413, detail=f"tarball_too_large_{MAX_TARBALL_BYTES}")

    sha256_hex = hashlib.sha256(tarball_bytes).hexdigest()

    forks_root = _forks_dir().resolve()
    dest_dir = (forks_root / str(fork.user_id) / fork.slug).resolve()
    if not str(dest_dir).startswith(str(forks_root) + os.sep):
        raise HTTPException(status_code=422, detail="path_traversal_detected")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{semver}.tar.gz"
    dest_path.write_bytes(tarball_bytes)
    try:
        dest_path.chmod(0o640)
    except OSError:
        pass  # /tmp on some test runners rejects chmod — nonfatal

    version = ForkVersion(
        id=uuid4(),
        fork_id=fork.id,
        semver=semver,
        tarball_path=str(dest_path),
        tarball_size_bytes=size,
        checksum_sha256=sha256_hex,
        changelog=changelog,
    )
    db.add(version)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"version_exists: {semver}")

    fork.latest_version_id = version.id
    db.commit()
    db.refresh(version)

    return {
        "id": str(version.id),
        "fork_id": str(fork.id),
        "semver": version.semver,
        "tarball_size_bytes": int(version.tarball_size_bytes),
        "checksum_sha256": version.checksum_sha256,
        "changelog": version.changelog,
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


def _make_install_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="recipes-fork-install")


@router.get("/{fork_id}/install")
def install_fork(
    fork_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: TierContext = Depends(require_operator),
):
    """Issue an HMAC-signed download URL with 5-min TTL for the latest version."""
    fork = _resolve_owned_fork(db, ctx, fork_id)
    if fork.visibility is None:
        raise HTTPException(status_code=404, detail="fork_not_found")

    if not fork.latest_version_id:
        raise HTTPException(status_code=404, detail="no_versions")
    version = (
        db.query(ForkVersion)
        .filter(ForkVersion.id == fork.latest_version_id)
        .first()
    )
    if not version:
        raise HTTPException(status_code=404, detail="no_versions")

    serializer = _make_install_serializer()
    token = serializer.dumps({
        "fork_id": str(fork.id),
        "version_id": str(version.id),
    })

    public_origin = (
        getattr(settings, "PUBLIC_ORIGIN", None)
        or os.environ.get("RECIPES_PUBLIC_ORIGIN")
        or "https://recipes.wisechef.ai"
    )
    url = public_origin.rstrip("/") + f"/api/forks/_download?token={token}"
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=INSTALL_TOKEN_TTL_SECONDS)
    return {
        "fork_id": str(fork.id),
        "slug": fork.slug,
        "version": version.semver,
        "tarball_url": url,
        "checksum_sha256": version.checksum_sha256,
        "size_bytes": int(version.tarball_size_bytes),
        "expires_at": expires_at.isoformat(),
    }


@router.get("/_download")
def download_fork(token: str, db: Session = Depends(get_db)):
    """Verify the HMAC token and stream the tarball. Public — auth lives in
    the token itself (5-minute TTL, signed with SIGNING_SECRET + fork salt)."""
    serializer = _make_install_serializer()
    try:
        data = serializer.loads(token, max_age=INSTALL_TOKEN_TTL_SECONDS)
    except SignatureExpired:
        raise HTTPException(status_code=401, detail="token_expired")
    except BadSignature:
        raise HTTPException(status_code=401, detail="invalid_token")

    try:
        version_uuid = UUID(data["version_id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=401, detail="invalid_token")
    version = (
        db.query(ForkVersion)
        .filter(ForkVersion.id == version_uuid)
        .first()
    )
    if not version:
        raise HTTPException(status_code=404, detail="version_not_found")

    tar_path = Path(version.tarball_path)
    if not tar_path.is_file():
        raise HTTPException(status_code=404, detail="tarball_missing_on_disk")

    return FileResponse(
        path=str(tar_path),
        media_type="application/gzip",
        filename=f"{version.fork_id}-{version.semver}.tar.gz",
        headers={"X-Checksum-SHA256": version.checksum_sha256},
    )


@router.delete("/{fork_id}")
def delete_fork(
    fork_id: str,
    db: Session = Depends(get_db),
    ctx: TierContext = Depends(require_operator),
):
    """Soft-delete: visibility is set to NULL and readme is cleared. The row
    remains so version history (and audit) survives, but list_forks will
    skip it and the unique (user_id, slug) constraint still blocks revival
    via fresh fork until the slug is re-used."""
    fork = _resolve_owned_fork(db, ctx, fork_id)
    fork.visibility = None
    fork.readme = None
    db.commit()
    return {"id": str(fork.id), "deleted": True}
