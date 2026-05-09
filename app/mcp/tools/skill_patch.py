"""MCP tool: recipes_propose_skill_patch.

Submit a working patch to a recipes-marketplace skill, creating a draft PR
on wisechef-ai/recipes-api. Mirrors app/mcp/tools/feedback.py in structure.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from app import github_dispatch, feedback_ratelimit
from app.models import SkillPatch
from app.skill_patch_validation import (
    validate_path,
    scan_forbidden,
    check_size,
    canonical_hash,
)

logger = logging.getLogger(__name__)


def recipes_propose_skill_patch(
    db: Session,
    *,
    slug: str,
    base_version: str,
    files: list[dict[str, str]],  # [{"path": str, "content": str}, ...]
    rationale: str,
    evidence_install_id: Optional[str] = None,
    agent_id_anon: Optional[str] = None,
    api_key_id: Optional[str] = None,
) -> dict[str, Any]:
    """Submit a working patch (draft PR) to a recipes-marketplace skill on wisechef-ai/recipes-api.

    Use when you have ALREADY fixed a skill locally during install or use and
    want to ship the fix back so other agents do not hit the same bug.
    Allowed file paths: SKILL.md, references/*.md, templates/*.{yml,yaml,sh,env,md}.
    Script changes (scripts/*, install.sh, recipe.yaml) are NOT allowed here
    — describe those as a comment on the skill-error issue body instead.
    Hard limits: 3 files max, 200 lines per file, 600 lines total.
    Rate limited to 1 patch per 24h per (agent, skill).
    Returns dedup_hash and (eventually) pr_url.
    """
    if not slug:
        return {"ok": False, "error": "slug is required"}
    if not base_version:
        return {"ok": False, "error": "base_version is required"}
    if not files:
        return {"ok": False, "error": "files list is empty"}
    if not rationale or len(rationale) > 2000:
        return {"ok": False, "error": "rationale must be 1-2000 characters"}

    # R8: Size cap
    size_ok, size_reason = check_size(files)
    if not size_ok:
        return {"ok": False, "error": "size_limit", "detail": size_reason}

    # R1: Path allowlist
    for f in files:
        path_ok, path_reason = validate_path(f["path"])
        if not path_ok:
            return {"ok": False, "error": "path_not_allowed", "detail": path_reason}

    # R2: Forbidden token scan
    for f in files:
        hits = scan_forbidden(f["content"])
        if hits:
            return {
                "ok": False,
                "error": "forbidden_token",
                "detail": (
                    f"File '{f['path']}' contains forbidden patterns: {hits}. "
                    "Supply-chain safety rejects these patterns unconditionally."
                ),
            }

    # R3: Rate limit (1/24h per (identity, slug))
    identity_base = f"api_key:{api_key_id}" if api_key_id else (
        f"agent:{agent_id_anon}" if agent_id_anon else "unknown"
    )
    identity = f"{identity_base}|{slug}"
    dedup_h = canonical_hash(slug, files)

    rl = feedback_ratelimit.check_and_record(
        identity=identity,
        tool="skill-patch",
        signature=dedup_h,
    )

    if not rl.allowed:
        if rl.loop_block:
            return {
                "ok": False,
                "error": "loop_detector_cooldown",
                "retry_at": rl.retry_at.isoformat() if rl.retry_at else None,
            }
        return {
            "ok": False,
            "error": "rate_limit_exceeded",
            "detail": "Maximum 1 patch per 24h per (agent, skill).",
        }

    # R4: Dedup
    existing = db.query(SkillPatch).filter(
        SkillPatch.dedup_hash == dedup_h,
        SkillPatch.status.in_(["pending", "opened"]),
    ).first()
    if existing:
        return {
            "ok": True,
            "dedup_hit": True,
            "pr_url": existing.gh_pr_url,
            "dedup_hash": dedup_h,
        }

    # Persist
    anon_hash = hashlib.sha256(
        (agent_id_anon or str(api_key_id) or "anon").encode()
    ).hexdigest()

    row = SkillPatch(
        api_key_h=hashlib.sha256(str(api_key_id).encode()).hexdigest() if api_key_id else None,
        slug=slug,
        base_version=base_version,
        dedup_hash=dedup_h,
        file_paths_json=[f["path"] for f in files],
        anon_hash=anon_hash,
        rationale=rationale,
        evidence_install_id=evidence_install_id,
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # GitHub dispatch (best-effort)
    sig = hashlib.sha256(f"{slug}|{dedup_h}".encode()).hexdigest()
    github_dispatch.dispatch_event(
        "skill-patch",
        {
            "id": str(row.id),
            "slug": slug,
            "base_version": base_version,
            "files": files,
            "rationale": rationale,
            "anon_hash": anon_hash,
            "dedup_hash": dedup_h,
            "signature": sig,
            "evidence_install_id": evidence_install_id,
            "agent_id_anon": agent_id_anon,
        },
    )

    logger.info(
        "skill-patch MCP accepted: slug=%s dedup=%s anon=%s",
        slug, dedup_h[:12], anon_hash[:12],
    )
    return {
        "ok": True,
        "dedup_hash": dedup_h,
        "status": "pending",
        "message": "Patch dispatched. Draft PR will be created within 30s.",
    }
