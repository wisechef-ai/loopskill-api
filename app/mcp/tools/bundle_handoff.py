"""recipes_cookbook_handoff — transfer or fork a cookbook to a new owner.

loopclose_3005 Phase I.

Modes:
  transfer — moves bundle_owner to the new user in-place. The bundle UUID
             stays the same. The original owner loses access; the new owner
             gains it.

  fork     — creates a NEW cookbook owned by the new user with:
               - parent_cookbook_id  = source.id
               - synced_from_cookbook_id = source.id
             Only custom-added CookbookSkill rows are copied (these are the
             tailored/private skills). forked/overridden/disabled rows are
             not copied — those are catalog-sync concerns, not user lineage.

Authz:
  - Only the current cookbook owner (ctx.user_id == bundle_owner) or a  # compat-alias
    master-scope caller may hand off.
  - cbt_token / anonymous / fleet callers are always rejected.
  - The base system catalog (is_base=True) cannot be handed off.
  - The new owner must be a real User row (resolved by id OR by email).
  - Fails closed: unknown user, unknown cookbook, wrong owner → error dict.

DB invariant: after transfer, bundle_owner IS NOT NULL (required by the
CHECK constraint added in migration lc3005_x_cookbook_owner_ck). After fork,
the new cookbook is created with bundle_owner = new_owner.id immediately.  # compat-alias
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import Cookbook, CookbookSkill, User

#: Only these sources represent the owner's own customisation work.
_TAILORED_SOURCES = {"custom-added"}


def recipes_cookbook_handoff(
    db: Session,
    *,
    ctx: AuthContext | None = None,
    cookbook_id: str,
    new_owner_user_id: str | None = None,
    new_owner_email: str | None = None,
    mode: str = "transfer",
) -> dict[str, Any]:
    """Transfer or fork a cookbook to a new owner.

    Args:
        db: SQLAlchemy session.
        ctx: Caller's AuthContext. Only the cookbook owner or master may act.
        cookbook_id: UUID of the source cookbook (as str).
        new_owner_user_id: UUID of the destination user (str). Mutually optional
            with new_owner_email — at least one must be provided.
        new_owner_email: Email of the destination user. Resolved to a User row.
        mode: ``'transfer'`` (in-place owner swap) or ``'fork'`` (new cookbook
            with lineage links + custom-added skills copied).

    Returns:
        On success:
          transfer → ``{status, cookbook_id, new_owner_user_id}``
          fork     → ``{status, new_cookbook_id, parent_cookbook_id,
                        copied_skills, new_owner_user_id}``
        On error:
          ``{error: <code>, message: <str>}``
    """
    # ── authz: reject non-user / non-master callers immediately ─────────────
    if ctx is None:
        return {"error": "auth_required", "message": "Authentication required."}

    scope = ctx.scope
    if scope not in ("user", "master"):
        return {
            "error": "forbidden",
            "message": "Only the cookbook owner or a master-scope caller may hand off a cookbook.",
        }

    # ── validate mode ────────────────────────────────────────────────────────
    if mode not in ("transfer", "fork"):
        return {
            "error": "invalid_mode",
            "message": f"mode must be 'transfer' or 'fork', got {mode!r}.",
        }

    # ── resolve bundle (before new owner — auth-close: probe prevention) ──
    try:
        cid = UUID(cookbook_id)
    except (ValueError, TypeError):
        return {"error": "cookbook_not_found", "message": "Invalid cookbook_id."}

    cb = db.query(Cookbook).filter(Cookbook.id == cid).first()
    if cb is None:
        return {"error": "cookbook_not_found", "message": "Cookbook not found."}

    # Base catalog cannot be handed off (would violate invariants)
    if cb.is_base:
        return {
            "error": "cannot_handoff_base",
            "message": "The base system catalog cannot be handed off.",
        }

    # ── ownership check (uses authz.can_write_cookbook for single source of truth) ─
    if not authz.can_write_cookbook(ctx, cb):
        return {
            "error": "forbidden",
            "message": "Only the cookbook owner may hand off this cookbook.",
        }

    # ── resolve new owner ────────────────────────────────────────────────────
    if not new_owner_user_id and not new_owner_email:
        return {
            "error": "new_owner_required",
            "message": "Provide new_owner_user_id or new_owner_email.",
        }

    new_owner: User | None = None
    if new_owner_user_id:
        try:
            nuid = UUID(new_owner_user_id)
        except (ValueError, TypeError):
            return {"error": "new_owner_not_found", "message": "Invalid new_owner_user_id."}
        new_owner = db.query(User).filter(User.id == nuid).first()
    elif new_owner_email:
        new_owner = db.query(User).filter(User.email == new_owner_email).first()

    if new_owner is None:
        return {
            "error": "new_owner_not_found",
            "message": "The specified new owner does not exist.",
        }

    # ── dispatch ─────────────────────────────────────────────────────────────
    if mode == "transfer":
        return _do_transfer(db, cb, new_owner)
    else:  # mode == "fork"
        return _do_fork(db, cb, new_owner)


def _do_transfer(
    db: Session,
    cb: Cookbook,
    new_owner: User,
) -> dict[str, Any]:
    """In-place ownership swap."""
    cb.bundle_owner = new_owner.id
    db.commit()
    db.refresh(cb)
    return {
        "status": "transferred",
        "cookbook_id": str(cb.id),
        "new_owner_user_id": str(new_owner.id),
    }


def _do_fork(
    db: Session,
    source: Cookbook,
    new_owner: User,
) -> dict[str, Any]:
    """Create a new cookbook for new_owner with lineage + custom-added skills."""
    new_cb = Cookbook(
        id=uuid4(),
        name=source.name,
        description=source.description,
        is_base=False,
        bundle_owner=new_owner.id,
        parent_bundle_id=source.id,
        synced_from_bundle_id=source.id,
    )
    db.add(new_cb)
    db.flush()  # get new_cb.id

    # Copy only custom-added skills (tailored content, not catalog-sync rows)
    tailored = (
        db.query(CookbookSkill)
        .filter(
            CookbookSkill.bundle_id == source.id,  # compat-alias
            CookbookSkill.source.in_(_TAILORED_SOURCES),
        )
        .all()
    )

    for cs in tailored:
        new_cs = CookbookSkill(
            bundle_id=new_cb.id,
            skill_id=cs.skill_id,
            source=cs.source,
            pinned_version=cs.pinned_version,
        )
        db.add(new_cs)

    db.commit()
    db.refresh(new_cb)

    return {
        "status": "forked",
        "new_cookbook_id": str(new_cb.id),
        "parent_cookbook_id": str(source.id),
        "copied_skills": len(tailored),
        "new_owner_user_id": str(new_owner.id),
    }
