"""spotify_2607 Phase B — slug-based LIKE on all four artifact types.

Routes:
  POST   /api/skills/{slug}/like          (compat alias → engagement_routes.like_track)
  DELETE /api/skills/{slug}/like          (compat alias → engagement_routes.unlike_track)
  POST   /api/personalities/{slug}/like
  DELETE /api/personalities/{slug}/like
  POST   /api/loops/{slug}/like           (composite loop; /api/composite-loops/{slug}/like is an alias)
  DELETE /api/loops/{slug}/like
  POST   /api/bundles/{slug}/like         (= follow; reuses FollowedBundle, per §7 Q3)
  DELETE /api/bundles/{slug}/like

DELETION PASS (musk-5-step, reported in the PR body):
  The plan's §7 Q2 worried about "four near-identical like tables" and proposed
  a polymorphic ``artifact_likes`` table. Recon proved that fear was unfounded:
  - skills, personalities AND loops already share ONE representation — the
    typed Liked bundle join (BundleSkill/BundlePersonality/BundleCompositeLoop)
    via ``set_liked_artifact`` (PR #82, on main since 2026-07-11). There is no
    SkillLike-clone per type to consolidate.
  - bundles already have a like-shaped verb — FollowedBundle (PR #83).
  So instead of a new ``ArtifactLike`` table + migration + backfill, this phase
  DELETE-PASSES the proposal: zero new tables, zero new migrations. The only
  new surface is slug-based ROUTES that delegate to the existing services.
  Owners of each requirement:
    - skill like (slug)            : engagement_routes (UNCHANGED — compat alias)
    - personality/loop like (slug) : this module → set_liked_artifact
    - bundle like (= follow)       : this module → FollowedBundle (follow_routes shape)
    - tier/authz gate per type     : this module + library_service (extended gate)
    - vetted/community badging     : library_service (_federated_liked_skills)
  Deleted: the proposed ``ArtifactLike`` model + alembic migration (drafted then
  discarded after recon — see commit history / PR body for the audit).

OWNERSHIP BOUNDARY: this module is NEW. It does NOT edit engagement_routes.py
(the Phase A owned file) — the skill slug routes there remain the source of
truth and this module's /api/skills/{slug}/like is intentionally NOT
re-registered, so the live heart control's byte-identical contract is
preserved. The only edits to Phase-A-owned files are the smallest possible
additive authz gate extension in library_service.py (skill-only → all types)
and the additive ``provenance`` badge — both documented loudly above the
diff. bundle_routes.py (Phase C) is untouched.

NO CASCADING SAVES (§0 #8, pinned by test): liking a bundle follows the bundle
reference only — it does NOT create likes for the bundle's member skills /
personalities / loops. Spotify shipped cascade-by-accident and reverted it; we
pin the absence of cascade with ``test_liking_bundle_does_not_cascade``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Bundle, CompositeLoop, FollowedBundle, Personality

router = APIRouter(tags=["engagement"])


# ── Response schemas ──────────────────────────────────────────────────────


class LikeResponse(BaseModel):
    """Like round-trip response. Mirrors engagement_routes.LikeResponse shape."""

    liked: bool
    like_count: int = 0


class BundleLikeResponse(BaseModel):
    """Bundle like (= follow) response."""

    liked: bool
    bundle_id: str
    follower_count: int = 0


# ── Auth helper ────────────────────────────────────────────────────────────


def _require_user(request: Request) -> AuthContext:
    """Extract AuthContext and require user scope (or master)."""
    ctx: AuthContext | None = getattr(request.state, "auth_ctx", None)
    if ctx is None or (ctx.scope not in ("user", "master") or ctx.user_id is None):
        raise HTTPException(status_code=401, detail="Authentication required")
    return ctx


def _enforce_tier_gate(ctx: AuthContext, tier: str | None) -> None:
    """Reject an over-tier like.

    spotify_2607 Phase B deliverable #5: tier/authz gating must apply per
    artifact type, not just to skills. Personalities and loops carry a
    nullable ``tier`` column (default NULL = free). A free-tier caller may not
    like a pro-tier artifact — liking is the gateway to the deployable Liked
    bundle, so an over-tier like is an over-tier install one step removed.
    Master scope bypasses (resolves to pro_plus upstream).
    """
    if ctx.scope == "master":
        return
    if not authz.tier_rank_allows_install(ctx.tier, tier):
        raise HTTPException(
            status_code=403,
            detail=f"tier_gated: your tier ({ctx.tier or 'free'}) may not like this artifact",
        )


# ── Personality like (slug) ───────────────────────────────────────────────


def _personality_or_404(db: Session, slug: str) -> Personality:
    p = db.query(Personality).filter(Personality.slug == slug).first()
    if p is None or p.is_archived:
        raise HTTPException(status_code=404, detail="personality not found")
    return p


@router.post("/api/personalities/{slug}/like", response_model=LikeResponse)
def like_personality(slug: str, request: Request, db: Session = Depends(get_db)) -> LikeResponse:
    """Like a personality by slug. Writes to the typed Liked bundle."""
    ctx = _require_user(request)
    p = _personality_or_404(db, slug)
    _enforce_tier_gate(ctx, getattr(p, "tier", None))
    _do_like_personality(db, ctx, p, liked=True)
    return LikeResponse(liked=True, like_count=_personality_like_count(db, p))


@router.delete("/api/personalities/{slug}/like", response_model=LikeResponse)
def unlike_personality(slug: str, request: Request, db: Session = Depends(get_db)) -> LikeResponse:
    """Unlike a personality by slug."""
    ctx = _require_user(request)
    p = _personality_or_404(db, slug)
    _do_like_personality(db, ctx, p, liked=False)
    return LikeResponse(liked=False, like_count=_personality_like_count(db, p))


def _do_like_personality(db: Session, ctx: AuthContext, p: Personality, *, liked: bool) -> None:
    # Delegate to the shared chokepoint so the authz gate (can_read_personality)
    # and the typed Liked-bundle join are exercised identically to the UUID
    # route (POST /api/library/like). Local import avoids a circular import at
    # module load (library_service imports from bundle_routes).
    from app.library_service import LikedArtifactForbiddenError, set_liked_artifact

    try:
        set_liked_artifact(
            db,
            owner_id=ctx.user_id,
            artifact_type="personality",
            artifact_id=p.id,
            liked=liked,
            ctx=ctx if liked else None,
        )
    except LikedArtifactForbiddenError as exc:
        raise HTTPException(status_code=403, detail="Not permitted to like this personality") from exc


def _personality_like_count(db: Session, p: Personality) -> int:
    """Count how many users have this personality in their Liked bundle.

    The like is the typed liked-bundle join written by
    ``library_service.set_liked_artifact`` — it lands on the user's single
    Liked bundle (``Bundle.is_liked == True``). Counting ALL
    ``BundlePersonality`` rows would also pick up curated / base / ordinary
    bundles that happen to declare the personality as a member, inflating the
    number (Codex review, PR #144). Scope the join to Liked bundles only.
    """
    from app.models import BundlePersonality

    return (
        db.query(BundlePersonality)
        .join(Bundle, BundlePersonality.bundle_id == Bundle.id)
        .filter(BundlePersonality.personality_id == p.id, Bundle.is_liked.is_(True))
        .count()
    )


# ── Composite loop like (slug) ────────────────────────────────────────────


def _loop_or_404(db: Session, slug: str) -> CompositeLoop:
    cl = db.query(CompositeLoop).filter(CompositeLoop.slug == slug).first()
    if cl is None or cl.is_archived:
        raise HTTPException(status_code=404, detail="composite loop not found")
    return cl


@router.post("/api/loops/{slug}/like", response_model=LikeResponse)
def like_loop(slug: str, request: Request, db: Session = Depends(get_db)) -> LikeResponse:
    """Like a composite loop by slug."""
    ctx = _require_user(request)
    cl = _loop_or_404(db, slug)
    _enforce_tier_gate(ctx, getattr(cl, "tier", None))
    _do_like_loop(db, ctx, cl, liked=True)
    return LikeResponse(liked=True, like_count=_loop_like_count(db, cl))


@router.delete("/api/loops/{slug}/like", response_model=LikeResponse)
def unlike_loop(slug: str, request: Request, db: Session = Depends(get_db)) -> LikeResponse:
    """Unlike a composite loop by slug."""
    ctx = _require_user(request)
    cl = _loop_or_404(db, slug)
    _do_like_loop(db, ctx, cl, liked=False)
    return LikeResponse(liked=False, like_count=_loop_like_count(db, cl))


# Alias: /api/composite-loops/{slug}/like → same handlers (council §6: the new
# surface has its own prefix; we expose like on both so a client using either
# vocabulary can heart a loop).
@router.post("/api/composite-loops/{slug}/like", response_model=LikeResponse)
def like_composite_loop_alias(slug: str, request: Request, db: Session = Depends(get_db)) -> LikeResponse:
    return like_loop(slug, request, db)


@router.delete("/api/composite-loops/{slug}/like", response_model=LikeResponse)
def unlike_composite_loop_alias(slug: str, request: Request, db: Session = Depends(get_db)) -> LikeResponse:
    return unlike_loop(slug, request, db)


def _do_like_loop(db: Session, ctx: AuthContext, cl: CompositeLoop, *, liked: bool) -> None:
    from app.library_service import LikedArtifactForbiddenError, set_liked_artifact

    try:
        set_liked_artifact(
            db,
            owner_id=ctx.user_id,
            artifact_type="loop",
            artifact_id=cl.id,
            liked=liked,
            ctx=ctx if liked else None,
        )
    except LikedArtifactForbiddenError as exc:
        raise HTTPException(status_code=403, detail="Not permitted to like this loop") from exc


def _loop_like_count(db: Session, cl: CompositeLoop) -> int:
    """Count how many users have this loop in their Liked bundle.

    Same scoping as ``_personality_like_count``: count only joins against the
    user's Liked bundle (``Bundle.is_liked == True``), not every bundle that
    happens to declare the loop as a member (Codex review, PR #144).
    """
    from app.models import BundleCompositeLoop

    return (
        db.query(BundleCompositeLoop)
        .join(Bundle, BundleCompositeLoop.bundle_id == Bundle.id)
        .filter(BundleCompositeLoop.composite_loop_id == cl.id, Bundle.is_liked.is_(True))
        .count()
    )


# ── Bundle like (= follow) by slug ────────────────────────────────────────


def _bundle_or_404(db: Session, slug: str) -> Bundle:
    # Bundle.slug is nullable for private/unpublished bundles; a slug lookup
    # naturally 404s those, which is correct — you can only like a bundle that
    # has a public handle.
    b = db.query(Bundle).filter(Bundle.slug == slug).first()
    if b is None:
        raise HTTPException(status_code=404, detail="bundle_not_found")
    return b


# Bundle like (= follow) routes are mounted on BOTH surfaces (/api/bundles
# and /api/cookbooks) to satisfy the repo-wide dual-surface symmetry gate
# (test_loopskill_bundle_surface_symmetry). Bundle is the primary vocabulary;
# cookbook is retained as a backward-compat alias — same shape as
# follow_routes.py and bundle_routes.py.
_bundle_like = APIRouter()


@_bundle_like.post("/{slug}/like", response_model=BundleLikeResponse)
def like_bundle(slug: str, request: Request, db: Session = Depends(get_db)) -> BundleLikeResponse:
    """Like a bundle by slug.

    Per §7 Q3, liking a bundle = Spotify's "follow a playlist": it creates a
    FollowedBundle row (a read-only saved reference), NOT a like on each
    member. The existing /api/bundles/{bundle_id}/follow route requires the
    bundle to be public and rejects self-follow; this slug route applies the
    SAME rules (a private bundle cannot be liked; you cannot like your own
    bundle — use the heart on its contents instead) so the two verbs stay
    consistent. See test_liking_bundle_does_not_cascade for the no-cascade pin.
    """
    ctx = _require_user(request)
    b = _bundle_or_404(db, slug)
    # mesh_0408 W1 (P0). Unlike the other bundle sites, the owner-match below
    # is a DENY predicate (the self-follow guard), so tenant-scoping it would
    # LOOSEN this route. The cross-tenant defect here is narrower and lives one
    # line earlier: an account that runs several client orgs owns every one of
    # their bundles, so `400 cannot_like_own_bundle` vs `404 bundle_not_found`
    # told a key deployed at client B whether a given private slug exists in
    # client A. Deny first, indistinguishably from "no such bundle".
    # Public bundles are exempt: the catalog is cross-tenant on purpose.
    if b.visibility != "public" and authz.crosses_tenant(ctx, b):
        raise HTTPException(status_code=404, detail="bundle_not_found")
    if b.bundle_owner == ctx.user_id:
        raise HTTPException(status_code=400, detail="cannot_like_own_bundle")
    if b.visibility != "public":
        raise HTTPException(status_code=403, detail="bundle_not_likable")
    existing = (
        db.query(FollowedBundle)
        .filter(FollowedBundle.user_id == ctx.user_id, FollowedBundle.bundle_id == b.id)
        .first()
    )
    if existing is None:
        db.add(FollowedBundle(id=uuid4(), user_id=ctx.user_id, bundle_id=b.id))
        try:
            db.commit()
        except IntegrityError:
            # Rationale: concurrent re-likes race only on the idempotency unique constraint.
            db.rollback()
    _resync_follower_count(db, b.id)
    db.refresh(b)
    return BundleLikeResponse(liked=True, bundle_id=str(b.id), follower_count=b.follower_count or 0)


@_bundle_like.delete("/{slug}/like", response_model=BundleLikeResponse)
def unlike_bundle(slug: str, request: Request, db: Session = Depends(get_db)) -> BundleLikeResponse:
    """Unlike a bundle by slug (removes the follow)."""
    ctx = _require_user(request)
    b = _bundle_or_404(db, slug)
    (
        db.query(FollowedBundle)
        .filter(FollowedBundle.user_id == ctx.user_id, FollowedBundle.bundle_id == b.id)
        .delete(synchronize_session=False)
    )
    db.commit()
    _resync_follower_count(db, b.id)
    db.refresh(b)
    return BundleLikeResponse(liked=False, bundle_id=str(b.id), follower_count=b.follower_count or 0)


def _resync_follower_count(db: Session, bundle_id: UUID) -> None:
    """Recompute Bundle.follower_count from the followed_bundles source of truth.

    Mirrors follow_routes._resync_follower_count exactly (correct-by-
    construction under concurrent follow/unfollow). Local copy rather than an
    import to avoid coupling this module to follow_routes' private helper and
    to respect the Phase-A ownership boundary on follow_routes.py.
    """
    from sqlalchemy import func as _func

    count = (
        db.query(_func.count(FollowedBundle.id)).filter(FollowedBundle.bundle_id == bundle_id).scalar() or 0
    )
    db.query(Bundle).filter(Bundle.id == bundle_id).update(
        {"follower_count": count}, synchronize_session=False
    )
    db.commit()


# Dual-mount: /api/bundles is the primary surface, /api/cookbooks is the
# backward-compat alias. Both must expose the like verb or the surface-
# symmetry gate (test_loopskill_bundle_surface_symmetry) regresses.
router.include_router(_bundle_like, prefix="/api/bundles", tags=["bundles"])
router.include_router(_bundle_like, prefix="/api/cookbooks", tags=["cookbooks"])  # compat-alias
