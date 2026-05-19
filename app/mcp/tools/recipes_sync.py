"""recipes_sync — apply or preview pending skill version updates for a cookbook.

Default behaviour (``dry_run=False``): writes back ``pinned_version`` to the
latest semver for every outdated skill in the specified cookbook.  Returns the
same diff shape as ``dry_run=True`` plus ``applied=True`` and optional
tarball URLs the caller should pull.

With ``dry_run=True``: returns the diff only — no DB mutations.

Adam directive 2026-05-07: default ``dry_run=False`` is NON-NEGOTIABLE.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import Cookbook, CookbookSkill, Skill, SkillVersion


def _find_outdated_skills(db: Session, cookbook_id: UUID) -> list[dict[str, Any]]:
    """Return rows for skills in *cookbook_id* where pinned_version < latest.

    Each row dict has keys: skill_id, slug, pinned_version, latest.
    """
    # Subquery: latest semver per skill
    latest_sq = (
        db.query(
            SkillVersion.skill_id,
            func.max(SkillVersion.semver).label("latest_semver"),
        )
        .group_by(SkillVersion.skill_id)
        .subquery()
    )

    rows = (
        db.query(
            CookbookSkill.skill_id,
            Skill.slug,
            CookbookSkill.pinned_version,
            latest_sq.c.latest_semver.label("latest"),
        )
        .join(Skill, Skill.id == CookbookSkill.skill_id)
        .join(latest_sq, latest_sq.c.skill_id == Skill.id)
        .filter(
            CookbookSkill.cookbook_id == cookbook_id,
            # outdated when pinned is NULL or pinned != latest
            (CookbookSkill.pinned_version == None)  # noqa: E711
            | (CookbookSkill.pinned_version != latest_sq.c.latest_semver),
        )
        .all()
    )

    return [
        {
            "skill_id": r.skill_id,
            "slug": r.slug,
            "from": r.pinned_version,
            "to": r.latest,
        }
        for r in rows
    ]


def recipes_sync(
    db: Session,
    *,
    cookbook_id: str,
    dry_run: bool = False,
    caller: dict[str, Any] | None = None,  # kept for backwards compat; prefer ctx
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Synchronise a cookbook's skills to their latest published versions.

    Phase B (Issue #15):
    - (a) Uses db.commit() instead of db.flush() so writes persist.
    - (b) Checks authz.can_write_cookbook(ctx, cb) before mutating.
    """
    # Resolve AuthContext: prefer ctx, fall back to master for legacy callers
    if ctx is None:
        ctx = AuthContext(scope="master")

    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_cookbook_id", "cookbook_id": cookbook_id}

    # Verify cookbook exists
    cb = db.query(Cookbook).filter(Cookbook.id == cb_uuid).first()
    if not cb:
        return {"error": "not_found", "cookbook_id": cookbook_id}

    # Phase B (Issue #15b): cookbook ownership check
    if not authz.can_write_cookbook(ctx, cb):
        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}

    outdated = _find_outdated_skills(db, cb_uuid)

    if not outdated:
        return {
            "cookbook_id": cookbook_id,
            "changes": [],
            "applied": not dry_run,
            "message": "All skills are up to date.",
        }

    changes = [
        {
            "slug": o["slug"],
            "from": o["from"],
            "to": o["to"],
            "action": "update",
        }
        for o in outdated
    ]

    result: dict[str, Any] = {
        "cookbook_id": cookbook_id,
        "changes": changes,
    }

    if dry_run:
        return result

    # ── APPLY path (default) ─────────────────────────────────────────────
    for o in outdated:
        db.query(CookbookSkill).filter(
            CookbookSkill.cookbook_id == cb_uuid,
            CookbookSkill.skill_id == o["skill_id"],
        ).update({"pinned_version": o["to"]})

    db.commit()  # Phase B (Issue #15a): commit, not flush

    # Build tarball URLs for the updated skills (same logic as recipes_install)
    install_urls = _build_install_urls(db, outdated)
    result["applied"] = True
    if install_urls:
        result["install_urls"] = install_urls

    return result


def _build_install_urls(db: Session, outdated: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return one-shot tarball URLs for each updated skill."""
    from itsdangerous import URLSafeTimedSerializer

    from app.config import settings

    try:
        serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET)
    # Rationale: URLSafeTimedSerializer init can fail if SIGNING_SECRET is empty; return empty list
    except Exception:  # noqa: BLE001
        return []

    public_origin = (
        getattr(settings, "PUBLIC_ORIGIN", None)
        or os.environ.get("RECIPES_PUBLIC_ORIGIN")
        or "https://recipes.wisechef.ai"
    )

    urls: list[dict[str, str]] = []
    for o in outdated:
        skill = db.query(Skill).filter(Skill.id == o["skill_id"]).first()
        if not skill or not skill.versions:
            continue
        latest = skill.versions[0]
        token = serializer.dumps({"slug": o["slug"], "version_id": str(latest.id), "mode": "files"})
        urls.append(
            {
                "slug": o["slug"],
                "version": o["to"],
                "tarball_url": (public_origin.rstrip("/") + "/api/skills/_download?token=" + token),
            }
        )

    return urls
