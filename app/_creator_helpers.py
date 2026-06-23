"""Shared creator-resolution helper used by publisher_routes and recipify.

Extracted from publisher_routes.py:352-376 logic so both publish and recipify
share the same "look up or auto-create Creator row from an authenticated user"
path without duplication.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.models import Creator


def _resolve_or_create_creator(
    ctx: AuthContext | None,
    db: Session,
) -> Creator | None:
    """Return an existing Creator row for ctx.user_id, or create one on the fly.

    Returns None when:
    - ctx is None
    - ctx.user_id is None (master key / anonymous)

    Mirrors the logic at publisher_routes.py:352-376 exactly so both call-sites
    behave identically.
    """
    if ctx is None or ctx.user_id is None:
        return None

    user_id = ctx.user_id

    creator = db.query(Creator).filter(Creator.user_id == user_id).first()
    if creator is not None:
        return creator

    # Auto-create a Creator row for this user (same logic as publisher_routes)
    from app.models import User  # local import avoids circular import at module level

    user_obj = db.query(User).filter(User.id == user_id).first()
    if user_obj is None:
        # User row doesn't exist (anonymous bundle owner, test fixtures, etc.)
        # — cannot satisfy the FK constraint, so leave creator_id unset.
        return None
    creator_slug = str(user_id).replace("-", "")[:32]
    creator = Creator(
        id=uuid4(),
        user_id=user_id,
        name=user_obj.display_name,
        slug=creator_slug,
    )
    db.add(creator)
    db.flush()
    return creator
