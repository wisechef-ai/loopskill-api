"""recipes_cookbook_install — MCP tool for cookbook-scoped installs.

cookbook_share_2105 Phase F.

The "give a cookbook share-token to another agent and have it install all
your skills" offering needs a single MCP entry point. This tool wraps the
HTTP routes:

  - bulk install  → POST /api/cookbooks/{cookbook_id}/install
  - single skill  → GET  /api/cookbooks/{cookbook_id}/skills/{slug}/install

Auth handling (via the shared AuthContext):
  - cbt_token scope: ``cookbook_id`` is OPTIONAL — defaults to
    ``ctx.cookbook_scope``. An explicit ``cookbook_id`` that doesn't match
    the token's scope is rejected (404 token_scope_mismatch).
  - user/master scope: ``cookbook_id`` is REQUIRED. user-scope must own the
    cookbook; master can install from any cookbook.

Response shapes match the REST handlers exactly so the MCP/HTTP contracts
stay in sync — agents can switch transports without re-parsing payloads.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.config import settings
from app.models import Cookbook, CookbookSkill, Skill, SkillVersion


def _make_install_url(skill_slug: str, version_id: UUID, version_semver: str) -> str:
    """DRY copy of cookbook_routes._make_install_url so the MCP path uses the
    same salt + URL shape as the HTTP path. Salt MUST stay
    'recipes-skill-install' to verify against
    install_routes._download — see secfix_1905/I-followup.

    Why duplicated: cookbook_routes is FastAPI-route-shaped (HTTPException,
    Depends, Response) and pulling it in here would drag a fastapi import
    into a pure-MCP module. The two-line URL builder is small enough to
    duplicate; salt/secret are both centralised in settings so drift is
    bounded. Salt-parity regression test
    (test_secfix_1905_d_cookbook_install_url) covers the equality.
    """
    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="recipes-skill-install")
    token = serializer.dumps({"slug": skill_slug, "version_id": str(version_id), "mode": "install"})
    public_origin = (
        getattr(settings, "PUBLIC_ORIGIN", None)
        or os.environ.get("RECIPES_PUBLIC_ORIGIN")
        or "https://recipes.wisechef.ai"
    )
    return public_origin.rstrip("/") + "/api/skills/_download?token=" + token


class CookbookInstallError(Exception):
    """Raised when the MCP tool cannot resolve the install scope.

    Carries a ``code`` (machine-readable) and ``status`` (HTTP-equivalent
    status code) so the MCP server can map this to a structured error
    response.
    """

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _resolve_cookbook(
    db: Session,
    ctx: AuthContext,
    cookbook_id: str | None,
) -> Cookbook:
    """Pick the right cookbook for this caller.

    cbt_token  : cookbook_id optional; defaults to ctx.cookbook_scope.
                 Explicit cookbook_id must match (no cross-scope reads).
    user       : cookbook_id REQUIRED; must own.
    master     : cookbook_id REQUIRED; any cookbook.
    anonymous  : never permitted; raises auth_required.
    """
    if ctx.scope == "anonymous":
        raise CookbookInstallError("auth_required", "Authentication required.", status=401)

    if ctx.scope == "cbt_token":
        # Default to scope-bound cookbook
        if cookbook_id is None:
            if ctx.cookbook_scope is None:
                raise CookbookInstallError(
                    "cookbook_id_missing",
                    "cbt_token caller has no cookbook_scope; cannot infer cookbook_id.",
                    status=422,
                )
            target_id = ctx.cookbook_scope
        else:
            try:
                target_id = UUID(cookbook_id)
            except (ValueError, TypeError) as exc:
                raise CookbookInstallError("cookbook_not_found", "cookbook_not_found", status=404) from exc
            if target_id != ctx.cookbook_scope:
                raise CookbookInstallError(
                    "token_scope_mismatch",
                    "cookbook_id does not match the token's cookbook_scope.",
                    status=403,
                )
        cb = db.query(Cookbook).filter(Cookbook.id == target_id).first()
        if cb is None:
            raise CookbookInstallError("cookbook_not_found", "cookbook_not_found", status=404)
        return cb

    # user / master path: explicit cookbook_id required
    if cookbook_id is None:
        raise CookbookInstallError(
            "cookbook_id_required",
            "cookbook_id is required for non-share-token callers.",
            status=422,
        )
    try:
        target_id = UUID(cookbook_id)
    except (ValueError, TypeError) as exc:
        raise CookbookInstallError("cookbook_not_found", "cookbook_not_found", status=404) from exc

    cb = db.query(Cookbook).filter(Cookbook.id == target_id).first()
    if cb is None:
        raise CookbookInstallError("cookbook_not_found", "cookbook_not_found", status=404)

    if ctx.scope == "master":
        return cb
    if ctx.scope == "user" and ctx.user_id is not None and cb.bundle_owner == ctx.user_id:
        return cb

    # Default: 404 (no oracle for non-owners — keep parity with REST routes)
    raise CookbookInstallError("cookbook_not_found", "cookbook_not_found", status=404)


def _resolve_version(db: Session, skill: Skill, pinned_version: str | None) -> SkillVersion | None:
    """Return the right SkillVersion: pinned if found, else latest."""
    version: SkillVersion | None = None
    if pinned_version:
        version = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == pinned_version)
            .first()
        )
    if version is None:
        version = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill.id)
            .order_by(SkillVersion.created_at.desc())
            .first()
        )
    return version


def recipes_cookbook_install(
    *,
    db: Session,
    ctx: AuthContext,
    cookbook_id: str | None = None,
    slug: str | None = None,
) -> dict[str, Any]:
    """Install all skills in a cookbook (bulk) or one skill by slug.

    Args:
        db: SQLAlchemy session (required; MCP server hands one in via
            validate_key path same as recipes_install).
        ctx: AuthContext (required). cbt_token callers may omit ``cookbook_id``.
        cookbook_id: Cookbook UUID string. Optional for cbt_token (defaults
            to ctx.cookbook_scope), required for user/master.
        slug: Optional single-skill filter. When provided, returns one skill
            payload mirroring GET /api/skills/install. When omitted, returns
            the bulk payload mirroring POST /api/cookbooks/{id}/install.

    Returns:
        Single-skill shape ({slug, version, tarball_url, checksum_sha256,
        source}) when slug is provided, otherwise the bulk shape
        ({cookbook_id, name, skills: [...]}).

    Raises:
        CookbookInstallError with structured (code, message, status) on
        validation/authz failure. The MCP server adapter catches these and
        maps them to the {error, status, code} response envelope.
    """
    cb = _resolve_cookbook(db, ctx, cookbook_id)

    # portal_0610 R1 (§6.6/§6.7-L10): tier-ACCESS gate, owner-tier-scoped.
    # The MCP install path is the primary cbt_ client-agent surface — it MUST
    # honour the same tier gate as the HTTP routes. The cookbook OWNER's tier
    # governs (a free-owner cookbook never emits a Pro tarball). External skills
    # carry no tarball/tier contract and are not gated.
    from app.authz import tier_rank_allows_install
    from app._skill_helpers import _resolve_cookbook_owner_tier

    _owner_tier = _resolve_cookbook_owner_tier(db, cb)

    if slug is not None:
        # Single-skill path
        skill = db.query(Skill).filter(Skill.slug == slug).first()
        if skill is None:
            raise CookbookInstallError("skill_not_found", "skill_not_found", status=404)

        # SECURITY: gate via the shared predicate so the cookbook-scope clause
        # (Phase C) is consulted. authz.can_install with db threaded checks
        # the cookbook→skill membership for cbt_token callers.
        if not authz.can_install(ctx, skill, db=db):
            # No oracle: indistinguishable from "not in cookbook" / "private".
            raise CookbookInstallError("skill_not_in_cookbook", "skill_not_in_cookbook", status=404)

        cs = (
            db.query(CookbookSkill)
            .filter(
                CookbookSkill.bundle_id == cb.id,  # compat-alias
                CookbookSkill.skill_id == skill.id,
                CookbookSkill.source != "disabled",
            )
            .first()
        )
        if cs is None:
            raise CookbookInstallError("skill_not_in_cookbook", "skill_not_in_cookbook", status=404)

        # federation_0604 Unit 2 — external skill: resolve real SKILL.md from
        # origin (never rehosted) via the SHARED resolver. No SkillVersion.
        from app.services.cookbook_external import (
            descriptor_source_slug,
            is_external_skill,
            resolve_external_install,
        )

        # portal_0610 R1: owner-tier gate (single-skill → explicit 403-equivalent).
        # External skills carry no tarball/tier contract → not gated.
        if not is_external_skill(skill) and not tier_rank_allows_install(
            _owner_tier, getattr(skill, "tier", None)
        ):
            raise CookbookInstallError(
                "tier_insufficient",
                f"This skill requires {skill.tier or 'pro'} tier; the cookbook owner's plan does not include it.",
                status=403,
            )

        if is_external_skill(skill):
            src_slug = descriptor_source_slug(skill)
            if src_slug is None:
                raise CookbookInstallError(
                    "external_descriptor_missing", "external_descriptor_missing", status=404
                )
            payload = resolve_external_install(*src_slug)
            if payload is None:
                raise CookbookInstallError(
                    "external_skill_unresolvable", "external_skill_unresolvable", status=404
                )
            from app.services.provenance import record_install_with_provenance

            _ev, provenance_id = record_install_with_provenance(
                db, skill=skill, version_semver="external", request=None, source="mcp", cookbook_id=cb.id
            )
            db.commit()
            return {**payload, "external": True, "source": cs.source, "provenance_id": provenance_id}

        version = _resolve_version(db, skill, cs.pinned_version)
        if version is None:
            raise CookbookInstallError("no_versions", "no_versions", status=404)

        # spotify_0608 Ph E — record install + mint provenance (cookbook_id stamped).
        from app.services.provenance import record_install_with_provenance

        _ev, provenance_id = record_install_with_provenance(
            db, skill=skill, version_semver=version.semver, request=None, source="mcp", cookbook_id=cb.id
        )
        db.commit()

        return {
            "slug": skill.slug,
            "version": version.semver,
            "tarball_url": _make_install_url(skill.slug, version.id, version.semver),
            "checksum_sha256": version.checksum_sha256,
            "source": cs.source,
            "provenance_id": provenance_id,
        }

    # Bulk path
    rows = (
        db.query(CookbookSkill, Skill)
        .join(Skill, Skill.id == CookbookSkill.skill_id)
        .filter(CookbookSkill.bundle_id == cb.id, CookbookSkill.source != "disabled")  # compat-alias
        .all()
    )

    skills_payload: list[dict[str, Any]] = []
    installed: list[tuple[Skill, str, int]] = []
    for cs, skill in rows:
        # SECURITY: per-skill authz gate. For cbt_token callers this MUST pass
        # because the skill is in their scoped cookbook (the Phase C predicate
        # asks exactly that). The defensive call is still here so that a
        # private skill incorrectly added to a cookbook the caller doesn't
        # own (e.g. via master action) is filtered out rather than leaked.
        if not authz.can_install(ctx, skill, db=db):
            continue
        # federation_0604 Unit 2 — external rows: cheap descriptor + scoped URL,
        # no origin fetch in the bulk path (isolation wall #2).
        from app.services.cookbook_external import install_descriptor_for, is_external_skill

        if is_external_skill(skill):
            skills_payload.append(install_descriptor_for(str(cb.id), skill))
            continue
        # portal_0610 R1: skip skills the cookbook owner's tier cannot install.
        if not tier_rank_allows_install(_owner_tier, getattr(skill, "tier", None)):
            continue
        version = _resolve_version(db, skill, cs.pinned_version)
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
            installed.append((skill, version.semver, len(skills_payload) - 1))

    # spotify_0608 Ph E — record install events + mint a PER-SKILL provenance_id
    # for MCP-driven bulk installs (R4 nit (a): provenance rides per-skill under
    # skills[], not cookbook-top-level). Stamps cookbook_id for feedback routing.
    from app.services.provenance import record_install_with_provenance

    for skill, semver, idx in installed:
        _ev, provenance_id = record_install_with_provenance(
            db, skill=skill, version_semver=semver, request=None, source="mcp", cookbook_id=cb.id
        )
        skills_payload[idx]["provenance_id"] = provenance_id
    if installed:
        db.commit()

    return {
        "cookbook_id": str(cb.id),
        "name": cb.name,
        "skills": skills_payload,
    }
