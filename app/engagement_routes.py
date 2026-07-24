"""spotify_1507 Phase A — Engagement routes (likes, favourites, discover, library).

The Spotify-shaped engagement layer for LoopSkill tracks:
  POST   /api/skills/{slug}/like         — like a track (local or federated)
  DELETE /api/skills/{slug}/like          — unlike
  POST   /api/skills/{slug}/favourite     — save to library
  DELETE /api/skills/{slug}/favourite      — remove from library
  GET    /api/bundles/discover            — engagement-ranked public bundles
  GET    /api/me/library                  — likes + favourites + owned + followed

Likes work on LOCAL skills (by slug → skill_id) AND FEDERATED tracks
(federated_source + federated_slug identity). The stable track identity
is `source:slug` for federated, `skill_id` for local.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select, desc, and_
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.library_service import set_local_like_by_skill
from app.models import (
    Bundle,
    FollowedBundle,
    Skill,
    SkillFavourite,
    SkillLike,
)

router = APIRouter(tags=["engagement"])


def _require_user(request: Request) -> AuthContext:
    """Extract AuthContext and require user scope (or master)."""
    ctx: AuthContext = getattr(request.state, "auth_ctx", None)
    if ctx is None or (ctx.scope not in ("user", "master") or ctx.user_id is None):
        raise HTTPException(status_code=401, detail="Authentication required")
    return ctx


# ── Response schemas ──────────────────────────────────────────────────────


class LikeResponse(BaseModel):
    liked: bool
    like_count: int


class FavouriteResponse(BaseModel):
    favourited: bool


class LibraryResponse(BaseModel):
    likes: list[dict]
    favourites: list[dict]
    owned_bundles: list[dict]
    followed_bundles: list[dict]


# ── Like / Unlike ─────────────────────────────────────────────────────────


def _resolve_track_identity(db: Session, slug: str) -> tuple:
    """Resolve a slug to either a local skill_id or federated identity.

    For local skills, returns (skill_id, None, None).
    For federated tracks (slug format 'source__slug'), returns (None, source, fed_slug).
    Raises 404 if the slug doesn't match anything.
    """
    # Try local skill first
    skill = db.execute(select(Skill).where(Skill.slug == slug)).scalar_one_or_none()
    if skill is not None:
        return skill.id, None, None

    # Check for federated track identity (source__slug format)
    if "__" in slug:
        parts = slug.split("__", 1)
        source, fed_slug = parts[0], parts[1]
        return None, source, fed_slug

    raise HTTPException(status_code=404, detail=f"Track '{slug}' not found")


@router.post("/api/skills/{slug}/like", response_model=LikeResponse)
def like_track(slug: str, request: Request, db: Session = Depends(get_db)):
    """Like a track (local skill or federated). Requires user scope."""
    ctx: AuthContext = _require_user(request)
    skill_id, fed_source, fed_slug = _resolve_track_identity(db, slug)

    # Check existing
    existing_q = select(SkillLike).where(SkillLike.user_id == ctx.user_id)
    if skill_id is not None:
        existing_q = existing_q.where(SkillLike.skill_id == skill_id)
    else:
        existing_q = existing_q.where(
            and_(SkillLike.federated_source == fed_source, SkillLike.federated_slug == fed_slug)
        )
    existing = db.execute(existing_q).scalar_one_or_none()

    # ponytail_0724: a LOCAL like must also land in the caller's deployable
    # Liked bundle — that is where GET /api/library reads from, and it is what
    # makes the skill reconcile onto their agents. Without this the heart wrote
    # engagement-only state and the skill appeared NOWHERE in the library.
    # Federated likes deliberately stay engagement-only (we do not host them,
    # and BundleSkill drives authz.can_install + fleet reconcile).
    #
    # R3: staged BEFORE the single commit so the engagement row and the mirror
    # are ATOMIC. Committing the SkillLike first and mirroring after left a
    # window where a mirror failure produced exactly the divergence this fix
    # exists to remove. The mirror also runs when the like already existed, so
    # it self-heals rows created before this shipped.
    if skill_id is not None:
        skill_row = db.execute(select(Skill).where(Skill.id == skill_id)).scalar_one_or_none()
        if skill_row is not None:
            set_local_like_by_skill(db, owner_id=ctx.user_id, skill=skill_row, liked=True, ctx=ctx)

    if existing is None:
        like = SkillLike(
            user_id=ctx.user_id,
            skill_id=skill_id,
            federated_source=fed_source,
            federated_slug=fed_slug,
        )
        db.add(like)

    db.commit()

    # Count total likes for this track
    count_q = select(func.count(SkillLike.id))
    if skill_id is not None:
        count_q = count_q.where(SkillLike.skill_id == skill_id)
    else:
        count_q = count_q.where(
            and_(SkillLike.federated_source == fed_source, SkillLike.federated_slug == fed_slug)
        )
    like_count = db.execute(count_q).scalar() or 0

    return LikeResponse(liked=True, like_count=like_count)


@router.delete("/api/skills/{slug}/like", response_model=LikeResponse)
def unlike_track(slug: str, request: Request, db: Session = Depends(get_db)):
    """Unlike a track. Requires user scope."""
    ctx: AuthContext = _require_user(request)
    skill_id, fed_source, fed_slug = _resolve_track_identity(db, slug)

    del_q = select(SkillLike).where(SkillLike.user_id == ctx.user_id)
    if skill_id is not None:
        del_q = del_q.where(SkillLike.skill_id == skill_id)
    else:
        del_q = del_q.where(
            and_(SkillLike.federated_source == fed_source, SkillLike.federated_slug == fed_slug)
        )
    existing = db.execute(del_q).scalar_one_or_none()
    if existing is not None:
        db.delete(existing)

    # ponytail_0724: mirror the removal out of the deployable Liked bundle so an
    # unlike actually removes the skill from the library (and from what
    # reconcile deploys). Runs unconditionally so it also cleans up a bundle
    # reference whose SkillLike row was already gone.
    # R3: staged before the SINGLE commit below so both sides are atomic.
    if skill_id is not None:
        skill_row = db.execute(select(Skill).where(Skill.id == skill_id)).scalar_one_or_none()
        if skill_row is not None:
            set_local_like_by_skill(db, owner_id=ctx.user_id, skill=skill_row, liked=False)

    db.commit()

    # Count remaining likes
    count_q = select(func.count(SkillLike.id))
    if skill_id is not None:
        count_q = count_q.where(SkillLike.skill_id == skill_id)
    else:
        count_q = count_q.where(
            and_(SkillLike.federated_source == fed_source, SkillLike.federated_slug == fed_slug)
        )
    like_count = db.execute(count_q).scalar() or 0

    return LikeResponse(liked=False, like_count=like_count)


# ── Favourite / Unfavourite ───────────────────────────────────────────────


@router.post("/api/skills/{slug}/favourite", response_model=FavouriteResponse)
def favourite_track(slug: str, request: Request, db: Session = Depends(get_db)):
    """Save a track to library (favourite). Requires user scope."""
    ctx: AuthContext = _require_user(request)
    skill_id, fed_source, fed_slug = _resolve_track_identity(db, slug)

    existing_q = select(SkillFavourite).where(SkillFavourite.user_id == ctx.user_id)
    if skill_id is not None:
        existing_q = existing_q.where(SkillFavourite.skill_id == skill_id)
    else:
        existing_q = existing_q.where(
            and_(
                SkillFavourite.federated_source == fed_source,
                SkillFavourite.federated_slug == fed_slug,
            )
        )
    existing = db.execute(existing_q).scalar_one_or_none()

    if existing is None:
        fav = SkillFavourite(
            user_id=ctx.user_id,
            skill_id=skill_id,
            federated_source=fed_source,
            federated_slug=fed_slug,
        )
        db.add(fav)
        db.commit()

    return FavouriteResponse(favourited=True)


@router.delete("/api/skills/{slug}/favourite", response_model=FavouriteResponse)
def unfavourite_track(slug: str, request: Request, db: Session = Depends(get_db)):
    """Remove a track from library. Requires user scope."""
    ctx: AuthContext = _require_user(request)
    skill_id, fed_source, fed_slug = _resolve_track_identity(db, slug)

    del_q = select(SkillFavourite).where(SkillFavourite.user_id == ctx.user_id)
    if skill_id is not None:
        del_q = del_q.where(SkillFavourite.skill_id == skill_id)
    else:
        del_q = del_q.where(
            and_(
                SkillFavourite.federated_source == fed_source,
                SkillFavourite.federated_slug == fed_slug,
            )
        )
    existing = db.execute(del_q).scalar_one_or_none()
    if existing is not None:
        db.delete(existing)
        db.commit()

    return FavouriteResponse(favourited=False)


# ── Bundle Discover ───────────────────────────────────────────────────────
# NOTE: the public discover FEED lives in app/bundle_routes.py (spotify_0608
# Ph B, mounted at GET /api/bundles/discover). spotify_1507 Ph A EXTENDS that
# route with a sort='engagement' mode + follower_count/is_editorial/curated_by
# fields rather than duplicating it here (one route, one source of truth).


# ── My Library ────────────────────────────────────────────────────────────


@router.get("/api/me/library", response_model=LibraryResponse)
def my_library(request: Request, db: Session = Depends(get_db)):
    """User's library: likes + favourites + owned bundles + followed bundles.

    Requires user scope.
    """
    ctx: AuthContext = _require_user(request)

    # Likes
    likes = (
        db.execute(
            select(SkillLike).where(SkillLike.user_id == ctx.user_id).order_by(desc(SkillLike.liked_at))
        )
        .scalars()
        .all()
    )

    # Favourites
    favourites = (
        db.execute(
            select(SkillFavourite)
            .where(SkillFavourite.user_id == ctx.user_id)
            .order_by(desc(SkillFavourite.favourited_at))
        )
        .scalars()
        .all()
    )

    # Owned bundles
    # ponytail_0724: exclude SYSTEM bundles. `is_base` was already excluded;
    # `is_liked` must be too — the Liked bundle is a per-user system primitive
    # (auto-created by ensure_liked_bundle), not a bundle the user composed.
    # This surfaced when the heart's local-like path started calling
    # ensure_liked_bundle: the auto-created Liked bundle began appearing in
    # `owned_bundles`. Same class as the liked_0711-P5 quota-counting fix.
    owned = (
        db.execute(
            select(Bundle)
            .where(
                and_(
                    Bundle.bundle_owner == ctx.user_id,
                    Bundle.is_base.is_(False),
                    Bundle.is_liked.is_(False),
                )
            )
            .order_by(desc(Bundle.updated_at))
        )
        .scalars()
        .all()
    )

    # Followed bundles
    followed_ids = (
        db.execute(select(FollowedBundle.bundle_id).where(FollowedBundle.user_id == ctx.user_id))
        .scalars()
        .all()
    )
    followed = []
    if followed_ids:
        followed = (
            db.execute(select(Bundle).where(Bundle.id.in_(followed_ids)).order_by(desc(Bundle.updated_at)))
            .scalars()
            .all()
        )

    def _track_out(like_or_fav):
        """Serialize a like/favourite to a track dict."""
        if like_or_fav.skill_id is not None:
            skill = db.execute(select(Skill).where(Skill.id == like_or_fav.skill_id)).scalar_one_or_none()
            if skill:
                return {"slug": skill.slug, "title": skill.title, "source": "local"}
        return {
            "slug": like_or_fav.federated_slug,
            "source": like_or_fav.federated_source,
            "title": like_or_fav.federated_slug,
        }

    def _bundle_out(b):
        return {
            "id": str(b.id),
            "name": b.name,
            "slug": b.slug,
            "description": b.description,
            "follower_count": b.follower_count,
        }

    return LibraryResponse(
        likes=[_track_out(l) for l in likes],
        favourites=[_track_out(f) for f in favourites],
        owned_bundles=[_bundle_out(b) for b in owned],
        followed_bundles=[_bundle_out(b) for b in followed],
    )
