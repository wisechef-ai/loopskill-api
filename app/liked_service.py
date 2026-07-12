"""Provision the per-user Liked bundle."""

from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Bundle


def ensure_liked_bundle(db: Session, owner_id: UUID) -> Bundle:
    """Return an owner's Liked bundle, creating it safely when absent.

    The nested transaction keeps the caller's transaction usable when two
    sessions race and the database's per-owner partial unique index rejects
    the losing insert.
    """
    existing = db.query(Bundle).filter(Bundle.bundle_owner == owner_id, Bundle.is_liked.is_(True)).first()
    if existing is not None:
        return existing

    liked = Bundle(
        id=uuid4(),
        name="Liked",
        is_base=False,
        is_liked=True,
        bundle_owner=owner_id,
        visibility="private",
    )
    try:
        with db.begin_nested():
            db.add(liked)
            db.flush()
    except IntegrityError:
        existing = db.query(Bundle).filter(Bundle.bundle_owner == owner_id, Bundle.is_liked.is_(True)).first()
        if existing is None:
            raise
        return existing

    db.commit()
    db.refresh(liked)
    return liked
