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
from app.models import Cookbook, CookbookSkill, Skill


def _find_outdated_skills(db: Session, cookbook_id: UUID) -> list[dict[str, Any]]:
    """Return rows for skills in *cookbook_id* where pinned_version < latest.

    Each row dict has keys: skill_id, slug, pinned_version, latest.
    """
    # portal_0610 B2 — SEMANTIC latest per skill (SQL func.max(semver) is
    # lexicographic: max("1.9.0","1.10.0") wrongly returns "1.9.0", pinning
    # fleets to the OLDER version once a skill hits double-digit minors).
    from app.services.semver import latest_semver_for_skills

    declared = (
        db.query(
            CookbookSkill.skill_id,
            Skill.slug,
            CookbookSkill.pinned_version,
        )
        .join(Skill, Skill.id == CookbookSkill.skill_id)
        .filter(CookbookSkill.bundle_id == cookbook_id)  # compat-alias
        .all()
    )

    latest_by_skill = latest_semver_for_skills(db, {r.skill_id for r in declared})

    out: list[dict[str, Any]] = []
    for r in declared:
        latest = latest_by_skill.get(r.skill_id)
        if latest is None:
            continue  # skill has no published version — nothing to advance to
        if r.pinned_version is None or r.pinned_version != latest:
            out.append(
                {
                    "skill_id": r.skill_id,
                    "slug": r.slug,
                    "from": r.pinned_version,
                    "to": latest,
                }
            )
    return out


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

    # Verify bundle exists
    cb = db.query(Cookbook).filter(Cookbook.id == cb_uuid).first()
    if not cb:
        return {"error": "not_found", "cookbook_id": cookbook_id}

    # Phase B (Issue #15b): bundle ownership check
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
            CookbookSkill.bundle_id == cb_uuid,  # compat-alias
            CookbookSkill.skill_id == o["skill_id"],
        ).update({"pinned_version": o["to"]})

    # evergreen_0206 Phase A: a pin-write changes the bundle's declared state,
    # so advance the generation token (Bundle.updated_at). SQLAlchemy onupdate
    # does NOT fire on child CookbookSkill writes — bump the parent explicitly so
    # the cheap-poll 304-fast-path (Phase D) stays truthful. Only on the apply
    # path with real outdated rows; a no-op sync returns early above and never
    # reaches here, so the token never falsely advances.
    db.query(Cookbook).filter(Cookbook.id == cb_uuid).update(
        {"updated_at": func.now()}, synchronize_session=False
    )

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
        # Issue #27 (secfix_1905/I-followup): salt MUST match install_routes._verify_signed_token.
        # Phase 3+4: primary salt changed to "loopskill-install"; verifier accepts both.
        serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
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
