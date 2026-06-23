"""recipes_tailor — fork a skill and get an install URL (one MCP call).

# Public-scope MCP tool: authz is handled by the underlying REST route
# (forks_routes.require_operator) which enforces Pro-tier subscription.
# The MCP-level functions additionally check ctx.user_id to reject master-key
# callers (who have no user_id to own a fork).

Combines the "fork + version + install" flow into a single tool so agents
can tailor a public skill without understanding the multi-step REST API.

integrator_2905 W1: exposed at Pro tier (not pro_plus) for broader first-dollar
funnel. The tier gate is enforced by the underlying REST route
(forks_routes.require_operator) which now accepts Pro.

Also provides `recipes_fork_list` to enumerate the caller's existing forks.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.models import Skill, SkillFork


def recipes_fork_list(
    db: Session,
    *,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """List all forks owned by the authenticated user.

    Returns the same shape as GET /api/forks/list but via MCP transport.
    Master-key callers see an empty list (no user_id to filter by).
    """
    if ctx is None or ctx.user_id is None:
        return {"forks": []}

    rows = (
        db.query(SkillFork)
        .filter(
            SkillFork.user_id == ctx.user_id,
            SkillFork.visibility.isnot(None),
        )
        .order_by(SkillFork.created_at.desc())
        .all()
    )

    source_ids = {r.source_skill_id for r in rows}
    sources = db.query(Skill.id, Skill.slug).filter(Skill.id.in_(source_ids)).all() if source_ids else []
    by_id = {sid: slug for sid, slug in sources}

    return {
        "forks": [
            {
                "id": str(r.id),
                "name": r.name,
                "slug": r.slug,
                "source_slug": by_id.get(r.source_skill_id),
                "visibility": r.visibility,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "latest_version_id": str(r.latest_version_id) if r.latest_version_id else None,
            }
            for r in rows
        ]
    }


def recipes_tailor(
    db: Session,
    *,
    source_slug: str,
    name: str,
    readme: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Fork a public skill for the authenticated user.

    Creates a private SkillFork of the given public skill. The fork is an
    editable copy that the user can modify and version independently.

    Returns the fork metadata including its ID and slug. Does NOT auto-version
    (the caller must upload a tarball via POST /api/forks/{id}/version before
    the fork can be installed).

    Tier: Pro or above (enforced by REST route, not duplicated here).
    """
    if ctx is None or ctx.user_id is None:
        return {"error": "auth_required", "message": "Must be authenticated as a user (not master key)"}

    # Resolve source skill
    source = db.query(Skill).filter(Skill.slug == source_slug).first()
    if not source or not source.is_public:
        return {"error": "source_not_found", "slug": source_slug}

    # Check for existing fork (same user, same source — idempotent return)
    import re
    from uuid import uuid4

    _SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

    def _slugify(n: str) -> str:
        s = re.sub(r"[^a-z0-9]+", "-", n.strip().lower()).strip("-")
        return s[:64] or "fork"

    slug = _slugify(name)

    existing = (
        db.query(SkillFork)
        .filter(
            SkillFork.user_id == ctx.user_id,
            SkillFork.source_skill_id == source.id,
            SkillFork.visibility.isnot(None),
        )
        .first()
    )
    if existing:
        return {
            "status": "existing",
            "fork_id": str(existing.id),
            "fork_slug": existing.slug,
            "name": existing.name,
            "source_slug": source_slug,
            "message": f"Fork already exists for this user+source (slug: {existing.slug})",
        }

    if not _SLUG_RE.match(slug):
        return {"error": "invalid_name", "message": f"Name produces invalid slug: {slug!r}"}

    from sqlalchemy.exc import IntegrityError

    fork = SkillFork(
        id=uuid4(),
        user_id=ctx.user_id,
        source_skill_id=source.id,
        name=name.strip(),
        slug=slug,
        readme=readme,
        visibility="private",
    )
    db.add(fork)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"error": "fork_exists", "slug": slug, "message": f"You already have a fork named {slug!r}"}
    db.refresh(fork)

    return {
        "status": "forked",
        "fork_id": str(fork.id),
        "fork_slug": fork.slug,
        "name": fork.name,
        "source_slug": source_slug,
        "message": "Fork created. Upload a version via POST /api/forks/{fork_id}/version to make it installable.",
    }
