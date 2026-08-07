"""loopskill_list_bundle — list a caller's cookbook + skill provenance.

Phase A only ships a read path against the existing ``Bundle`` /
``CookbookSkill`` tables (added in PR #19). The full CRUD endpoints are
Phase B's responsibility.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import Bundle, BundleSkill, Skill


def _coerce_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def loopskill_list_bundle(
    db: Session,
    *,
    ctx: AuthContext | None = None,
    user_id: Any | None = None,
    cookbook_id: str | None = None,
) -> dict[str, Any]:
    """Return a cookbook and its skill provenance rows.

    Authz gated: this tool is NOT public-scope — with an explicit
    cookbook_id it can return another user's private bundle contents
    (owner id, name, full skill list + pinned versions), so a real
    ownership check is required, not a comment. Gated via
    authz.can_read_cookbook — the SAME ownership/org predicate the REST
    bundle-detail route uses (app.bundle_routes._resolve_owned_cookbook,
    allow_org_read=True) — so MCP and REST agree on who may read a bundle.
    Unauthorized/nonexistent both return cookbook_not_found (no existence
    oracle), mirroring the REST route's docstring contract.
    """
    cookbook = None
    if cookbook_id:
        cb_uuid = _coerce_uuid(cookbook_id)
        cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first() if cb_uuid is not None else None
        if cb is None or ctx is None or not authz.can_read_cookbook(ctx, cb, allow_org_read=True):
            return {"error": "cookbook_not_found", "status": 404}
        cookbook = cb
    else:
        owner = _coerce_uuid(user_id) if user_id is not None else (ctx.user_id if ctx is not None else None)
        if owner is not None:
            # mesh_0408 W1 (P0), codex review of PR #202 finding 2. The
            # cookbook_id branch above is gated by can_read_cookbook, but this
            # implicit "your newest bundle" branch selected on a bare
            # owner-match, so an agent at client B calling
            # loopskill_list_bundle() with no argument got whichever client's
            # bundle the account touched most recently — name, owner and the
            # full skill list with pinned versions. Scoped to the caller's
            # tenant through the same clause the REST bundle list uses.
            # Built from the RESOLVED owner (the user_id kwarg wins over ctx for
            # legacy callers) plus the caller's tenant, so an explicit user_id
            # keeps selecting that user's bundles and gains the org clause.
            scope_ctx = SimpleNamespace(user_id=owner, org_id=getattr(ctx, "org_id", None))
            cookbook = (
                db.query(Bundle)
                .filter(authz.owner_match_within_tenant_clause(scope_ctx, Bundle))
                .order_by(Bundle.created_at.desc())
                .first()
            )

    if cookbook is None:
        return {"cookbook": None, "skills": []}

    rows = (
        db.query(BundleSkill, Skill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cookbook.id)  # compat-alias
        .all()
    )

    return {
        "cookbook": {
            "id": str(cookbook.id),
            "name": cookbook.name,
            "is_base": bool(cookbook.is_base),
            "parent_cookbook_id": (  # compat-alias: legacy field kept for MCP client compat
                str(cookbook.parent_bundle_id) if cookbook.parent_bundle_id else None  # compat-alias
            ),
            "owner": (str(cookbook.bundle_owner) if cookbook.bundle_owner else None),  # compat-alias
        },
        "skills": [
            {
                "skill_id": str(skill.id),
                "slug": skill.slug,
                "title": skill.title,
                "source": cs.source,
                "pinned_version": cs.pinned_version,
            }
            for cs, skill in rows
        ],
    }
