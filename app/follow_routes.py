"""HTTP API for following public bundles as read-only saved references."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Bundle, FollowedBundle

_h = APIRouter(tags=["bundles"])


def _authenticated_user_id(request: Request) -> UUID:
    """Require a user identity because follows belong to one user."""
    ctx = getattr(request.state, "auth_ctx", None)
    if not isinstance(ctx, AuthContext) or ctx.user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return ctx.user_id


def _bundle_or_404(db: Session, bundle_id: str) -> Bundle:
    """Resolve a bundle by UUID without treating malformed IDs as server errors."""
    try:
        parsed_id = UUID(bundle_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="bundle_not_found") from exc
    bundle = db.query(Bundle).filter(Bundle.id == parsed_id).first()
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle_not_found")
    return bundle


@_h.post("/{bundle_id}/follow")
def follow_bundle(bundle_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Idempotently follow a public bundle without copying its content."""
    user_id = _authenticated_user_id(request)
    bundle = _bundle_or_404(db, bundle_id)
    if bundle.bundle_owner == user_id:
        raise HTTPException(status_code=400, detail="cannot_follow_own_bundle")
    if bundle.visibility != "public":
        raise HTTPException(status_code=403, detail="bundle_not_followable")

    existing = (
        db.query(FollowedBundle)
        .filter(FollowedBundle.user_id == user_id, FollowedBundle.bundle_id == bundle.id)
        .first()
    )
    if existing is None:
        db.add(FollowedBundle(id=uuid4(), user_id=user_id, bundle_id=bundle.id))
        try:
            db.commit()
        except IntegrityError:
            # Rationale: concurrent re-follows race only on the idempotency unique constraint.
            db.rollback()
    return {"following": True, "bundle_id": str(bundle.id)}


@_h.delete("/{bundle_id}/follow")
def unfollow_bundle(bundle_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Idempotently remove a saved bundle reference."""
    user_id = _authenticated_user_id(request)
    bundle = _bundle_or_404(db, bundle_id)
    (
        db.query(FollowedBundle)
        .filter(FollowedBundle.user_id == user_id, FollowedBundle.bundle_id == bundle.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    return {"following": False, "bundle_id": str(bundle.id)}


# Bundle is the primary name; cookbook remains a compatibility alias for the
# project-wide dual-surface contract.
router = APIRouter()
router.include_router(_h, prefix="/api/bundles", tags=["bundles"])
router.include_router(_h, prefix="/api/cookbooks", tags=["cookbooks"])  # compat-alias
