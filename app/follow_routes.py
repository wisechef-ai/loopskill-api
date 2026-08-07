"""HTTP API for following public bundles as read-only saved references."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Bundle, FollowedBundle

_h = APIRouter(tags=["bundles"])


def _authenticated_ctx(request: Request) -> AuthContext:
    """Require a user identity because follows belong to one user."""
    ctx = getattr(request.state, "auth_ctx", None)
    if not isinstance(ctx, AuthContext) or ctx.user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return ctx


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
    ctx = _authenticated_ctx(request)
    user_id = ctx.user_id
    bundle = _bundle_or_404(db, bundle_id)
    # mesh_0408 W1 (P0), codex review of PR #202 finding 3. Identical treatment
    # to artifact_like_routes.like_bundle: the owner-match below is a DENY
    # predicate (the self-follow guard), so tenant-scoping it would LOOSEN this
    # route. The cross-tenant defect is one line earlier — an account owns every
    # bundle of every client org it runs, so `400 cannot_follow_own_bundle` vs
    # `404 bundle_not_found` told a key deployed at client B whether a given
    # private bundle id exists in client A. Deny first, indistinguishably from
    # "no such bundle". Public bundles stay exempt: the catalog is cross-tenant
    # on purpose, and following one is the whole point of the verb.
    if bundle.visibility != "public" and authz.crosses_tenant(ctx, bundle):
        raise HTTPException(status_code=404, detail="bundle_not_found")
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
            # spotify_1507 Ph A — maintain the denormalized follower_count so the
            # engagement-ranked discover feed doesn't re-count per request. Recount
            # from the source table (authoritative) rather than a blind += to stay
            # correct under concurrent (un)follows.
            _resync_follower_count(db, bundle.id)
        except IntegrityError:
            # Rationale: concurrent re-follows race only on the idempotency unique constraint.
            db.rollback()
    return {"following": True, "bundle_id": str(bundle.id)}


def _resync_follower_count(db: Session, bundle_id) -> None:
    """Recompute Bundle.follower_count from the followed_bundles source of truth.

    Denormalized counter kept correct-by-construction: recount rather than
    increment so concurrent follow/unfollow can't drift it (spotify_1507 Ph A).
    """
    from sqlalchemy import func as _func

    count = (
        db.query(_func.count(FollowedBundle.id)).filter(FollowedBundle.bundle_id == bundle_id).scalar() or 0
    )
    db.query(Bundle).filter(Bundle.id == bundle_id).update(
        {"follower_count": count}, synchronize_session=False
    )
    db.commit()


@_h.delete("/{bundle_id}/follow")
def unfollow_bundle(bundle_id: str, request: Request, db: Session = Depends(get_db)) -> dict:
    """Idempotently remove a saved bundle reference."""
    ctx = _authenticated_ctx(request)
    user_id = ctx.user_id
    bundle = _bundle_or_404(db, bundle_id)
    existing = (
        db.query(FollowedBundle)
        .filter(FollowedBundle.user_id == user_id, FollowedBundle.bundle_id == bundle.id)
        .first()
    )
    # Same no-oracle rule as follow, one step weaker so it cannot strand a row:
    # a caller who ALREADY follows this bundle (it was public when they followed
    # and has since gone private) must keep the ability to unfollow. Everyone
    # else gets the not-found answer for another tenant's private bundle, so
    # 200-vs-404 stops reporting whether the id is real.
    if existing is None and bundle.visibility != "public" and authz.crosses_tenant(ctx, bundle):
        raise HTTPException(status_code=404, detail="bundle_not_found")
    if existing is not None:
        db.delete(existing)
    db.commit()
    # spotify_1507 Ph A — keep the denormalized counter in sync on unfollow too.
    _resync_follower_count(db, bundle.id)
    return {"following": False, "bundle_id": str(bundle.id)}


# Bundle is the primary name; cookbook remains a compatibility alias for the
# project-wide dual-surface contract.
router = APIRouter()
router.include_router(_h, prefix="/api/bundles", tags=["bundles"])
router.include_router(_h, prefix="/api/cookbooks", tags=["cookbooks"])  # compat-alias
