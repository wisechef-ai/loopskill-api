"""POST /api/v1/skill-patch — submit a working skill patch as a draft PR.

Validation pipeline (fail-fast, 400 on any failure):
  R8 → R1 → R2 → R3 → R4 → insert + dispatch

Auth: x-api-key via APIKeyMiddleware (standard rec_* key).
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import feedback_ratelimit, github_dispatch
from app.database import get_db
from app.models import SkillPatch
from app.skill_patch_validation import (
    canonical_hash,
    check_size,
    scan_forbidden,
    validate_path,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["skill-patch"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_identity(request: Request, agent_id: str | None = None) -> str:
    """Resolve caller identity for rate-limiting (same pattern as feedback_v1_routes)."""
    state = getattr(request, "state", None)
    if state:
        api_key_id = getattr(state, "api_key_id", None)
        if api_key_id:
            return str(api_key_id)
    if agent_id:
        return f"agent:{agent_id}"
    forwarded = getattr(getattr(request, "headers", None), "get", lambda k, d="": d)("x-forwarded-for", "")
    if forwarded:
        return f"ip:{forwarded.split(',')[0].strip()}"
    client = getattr(request, "client", None)
    if client:
        return f"ip:{client.host}"
    return "ip:unknown"


def _get_api_key_h(request: Request) -> str:
    """Return a hash of the API key header (anon fingerprint), or empty string."""
    key = request.headers.get("x-api-key", "")
    if key:
        return hashlib.sha256(key.encode()).hexdigest()
    return ""


# ── Schemas ───────────────────────────────────────────────────────────────────


class FileEntry(BaseModel):
    path: str
    content: str


class SkillPatchRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=128)
    base_version: str = Field(min_length=1, max_length=64)
    files: list[FileEntry]
    rationale: str = Field(min_length=1, max_length=2000)
    evidence_install_id: str | None = Field(default=None, max_length=128)
    agent_id_anon: str | None = Field(default=None, max_length=128)


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/skill-patch", status_code=status.HTTP_200_OK)
def post_skill_patch(
    payload: SkillPatchRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Submit a working skill patch and open a draft PR on wisechef-ai/recipes-api."""
    files_dicts = [{"path": f.path, "content": f.content} for f in payload.files]

    # ── R8: Size cap ────────────────────────────────────────────────────────
    size_ok, size_reason = check_size(files_dicts)
    if not size_ok:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "reason": "size_limit", "detail": size_reason},
        )

    # ── R1: Path allowlist ──────────────────────────────────────────────────
    for f in payload.files:
        path_ok, path_reason = validate_path(f.path)
        if not path_ok:
            raise HTTPException(
                status_code=400,
                detail={"ok": False, "reason": "path_not_allowed", "detail": path_reason},
            )

    # ── R2: Forbidden-token scan ────────────────────────────────────────────
    for f in payload.files:
        hits = scan_forbidden(f.content)
        if hits:
            raise HTTPException(
                status_code=400,
                detail={
                    "ok": False,
                    "reason": "forbidden_token",
                    "detail": (
                        f"File '{f.path}' contains forbidden patterns: {hits}. "
                        "Supply-chain safety rejects these patterns unconditionally."
                    ),
                },
            )

    # ── R3: Rate limit ──────────────────────────────────────────────────────
    identity = _get_identity(request, payload.agent_id_anon)
    # Use slug-scoped key so the 1/24h limit is per (identity, slug)
    rl_identity = f"{identity}|{payload.slug}"
    dedup_h = canonical_hash(payload.slug, files_dicts)

    # We use the dedup_hash as the RL signature so per-tool fires at 1/24h
    rl = feedback_ratelimit.check_and_record(
        identity=rl_identity,
        tool="skill-patch",
        signature=dedup_h,
    )

    if not rl.allowed:
        if rl.loop_block:
            raise HTTPException(
                status_code=429,
                detail={
                    "ok": False,
                    "error": "loop_detector_cooldown",
                    "retry_at": rl.retry_at.isoformat() if rl.retry_at else None,
                },
            )
        raise HTTPException(
            status_code=429,
            detail={
                "ok": False,
                "error": "rate_limit_exceeded",
                "detail": ("Maximum 1 patch per 24h per (agent, skill). Wait before re-submitting."),
            },
        )

    # ── R4: Dedup ────────────────────────────────────────────────────────────
    existing = (
        db.query(SkillPatch)
        .filter(
            SkillPatch.dedup_hash == dedup_h,
            SkillPatch.status.in_(["pending", "opened"]),
        )
        .first()
    )
    if existing:
        return {
            "ok": True,
            "dedup_hit": True,
            "pr_url": existing.gh_pr_url,
            "dedup_hash": dedup_h,
        }

    # ── Persist ──────────────────────────────────────────────────────────────
    api_key_h = _get_api_key_h(request)
    anon_hash = hashlib.sha256((payload.agent_id_anon or api_key_h or "anon").encode()).hexdigest()

    row = SkillPatch(
        api_key_h=api_key_h or None,
        slug=payload.slug,
        base_version=payload.base_version,
        dedup_hash=dedup_h,
        file_paths_json=[f.path for f in payload.files],
        anon_hash=anon_hash,
        rationale=payload.rationale,
        evidence_install_id=payload.evidence_install_id,
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # ── GitHub dispatch (best-effort) ────────────────────────────────────────
    sig = hashlib.sha256(f"{payload.slug}|{dedup_h}".encode()).hexdigest()
    github_dispatch.dispatch_event(
        "skill-patch",
        {
            "id": str(row.id),
            "slug": payload.slug,
            "base_version": payload.base_version,
            "files": [{"path": f.path, "content": f.content} for f in payload.files],
            "rationale": payload.rationale,
            "anon_hash": anon_hash,
            "dedup_hash": dedup_h,
            "signature": sig,
            "evidence_install_id": payload.evidence_install_id,
            "agent_id_anon": payload.agent_id_anon,
        },
    )

    logger.info(
        "skill-patch accepted: slug=%s dedup_hash=%s anon=%s",
        payload.slug,
        dedup_h[:12],
        anon_hash[:12],
    )
    return {
        "ok": True,
        "dedup_hash": dedup_h,
        "status": "pending",
        "message": "Patch dispatched. Draft PR will be created within 30s.",
    }
