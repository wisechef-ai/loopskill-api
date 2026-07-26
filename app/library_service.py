"""Liked-library reads and mutations shared by HTTP and MCP surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy.orm import Session

from app import authz
from app.bundle_routes import _touch_bundle_generation
from app.liked_service import ensure_liked_bundle
from app.models import (
    BundleCompositeLoop,
    BundlePersonality,
    BundleSkill,
    CompositeLoop,
    FollowedBundle,
    Personality,
    Skill,
    SkillLike,
    User,
)
from app.services.federated_titles import federated_title_for, resolve_federated_hub_titles

if TYPE_CHECKING:
    from app.auth_ctx import AuthContext

ArtifactType = Literal["skill", "personality", "loop"]


class LikedArtifactNotFoundError(Exception):
    """Raised when a requested liked-library artifact does not exist."""


class LikedArtifactForbiddenError(Exception):
    """Raised when the caller may not read the artifact they tried to like."""


def _artifact_spec(artifact_type: ArtifactType) -> tuple[type, type, str, dict[str, str]]:
    """Return the artifact model, join model, join ID field, and join defaults."""
    if artifact_type == "skill":
        return Skill, BundleSkill, "skill_id", {"source": "custom-added"}
    if artifact_type == "personality":
        return Personality, BundlePersonality, "personality_id", {}
    return CompositeLoop, BundleCompositeLoop, "composite_loop_id", {}


def set_liked_artifact(
    db: Session,
    *,
    owner_id: UUID,
    artifact_type: ArtifactType,
    artifact_id: UUID,
    liked: bool,
    ctx: AuthContext | None = None,
) -> dict[str, str | bool]:
    """Idempotently add or remove one artifact from the owner's Liked bundle.

    When ``ctx`` is supplied and the artifact is a skill, the caller must be
    able to READ that skill (authz.can_read_skill) — you cannot like a private
    skill you are not entitled to see. Personalities and loops are catalog-
    public artifacts, so no per-artifact read gate applies to them today.
    """
    bundle = ensure_liked_bundle(db, owner_id)
    artifact_model, join_model, artifact_id_field, join_defaults = _artifact_spec(artifact_type)
    artifact = db.query(artifact_model).filter(artifact_model.id == artifact_id).first()
    if artifact is None:
        raise LikedArtifactNotFoundError

    # authz gate (secfix_1905-B: every mutation surface carries an authz.can_* call).
    # Liking requires read-access to the artifact; unliking never needs a fresh
    # read (you are only removing a reference from your own bundle).
    #
    # spotify_2607 Phase B — the gate was previously skill-only, which left
    # personalities and loops un-gated (a caller could like a PRIVATE
    # personality/loop they cannot read, then read+install it via the Liked
    # bundle join — the exact privilege-escalation shape secfix_1905-B closed
    # for skills). can_read_personality / can_read_composite_loop (authz.py)
    # close the same hole for the two runnable types. Tier gating
    # (tier_rank_allows_install) is enforced by the caller in
    # artifact_like_routes._enforce_tier_gate, NOT here, because the typed
    # /api/library/like path is a library mutation, not an install — and
    # personalities/loops do not today carry a tier column that maps cleanly
    # onto TIER_RANK (they default to NULL = free). The slug routes in
    # artifact_like_routes apply the tier gate explicitly.
    if ctx is not None and liked:
        if artifact_type == "skill":
            if not authz.can_read_skill(ctx, artifact, db=db):
                raise LikedArtifactForbiddenError
        elif artifact_type == "personality":
            if not authz.can_read_personality(ctx, artifact, db=db):
                raise LikedArtifactForbiddenError
        elif artifact_type == "loop":
            if not authz.can_read_composite_loop(ctx, artifact, db=db):
                raise LikedArtifactForbiddenError

    existing = (
        db.query(join_model)
        .filter(join_model.bundle_id == bundle.id, getattr(join_model, artifact_id_field) == artifact_id)
        .first()
    )
    changed = False
    if liked and existing is None:
        db.add(
            join_model(
                bundle_id=bundle.id,
                **{artifact_id_field: artifact_id},
                **join_defaults,
            )
        )
        changed = True
    elif not liked and existing is not None:
        db.delete(existing)
        changed = True

    if changed:
        _touch_bundle_generation(db, bundle.id)
        db.commit()

    return {"liked": liked, "type": artifact_type, "id": str(artifact_id)}


def set_local_like_by_skill(
    db: Session,
    *,
    owner_id: UUID,
    skill: Skill,
    liked: bool,
    ctx: AuthContext | None = None,
) -> None:
    """Mirror a slug-route like of a LOCAL skill into the deployable Liked bundle.

    ponytail_0724 R2 self-audit. The browse/home heart posts to the SLUG route
    (``POST /api/skills/{slug}/like``) — it must, because a federated skill has
    no local UUID and so cannot use the UUID-based ``POST /api/library/like``.

    For a LOCAL catalog skill that route previously wrote only engagement state
    (``SkillLike(skill_id=...)``), while ``liked_library`` reads the
    ``BundleSkill`` join. Result: hearting a local skill put it NOWHERE the user
    could see it — the same lying-button bug as the federated case, on the more
    common path.

    So a local like is ALSO reflected into the caller's Liked bundle, which is
    the artifact's real destination (and what makes it reconcile onto their
    agents). Idempotent in both directions.

    AUTHORIZATION (R3 self-audit — this is a privilege-escalation surface).
    ``authz.can_read_skill`` grants access to a PRIVATE skill that lives in a
    bundle the caller owns (the loopclose_3005 Phase C bundle-ownership clause),
    and the Liked bundle is owned by the caller. Meanwhile the slug route's
    ``_resolve_track_identity`` does a BARE slug lookup with NO visibility gate.
    Chained, that would be: like a private skill you cannot read -> it enters
    your Liked bundle -> ``can_read_skill`` now returns True -> you can read and
    INSTALL it. So a LIKE is gated on ``authz.can_read_skill`` exactly as
    ``set_liked_artifact`` gates its own writes. Callers in this path MUST pass
    ``ctx``; without it we fail closed and write nothing.

    An UNLIKE is never gated — removing a reference from your own bundle cannot
    widen access — so ``ctx`` may be ``None`` there.

    TRANSACTIONS (R3 MEDIUM #2). This helper only ``flush()``es; the CALLER owns
    the commit, so the engagement row and this mirror land in ONE transaction
    (the routes stage both, then commit once). Residual, documented: the
    pre-existing ``ensure_liked_bundle`` commits when it CREATES a user's Liked
    bundle — a first-ever like therefore has a commit boundary before the join
    write. That is idempotent and self-healing (an empty Liked bundle is the
    steady state for every user anyway, and the next like re-runs the mirror),
    so it is not worth changing a helper shared by other callers.
    """
    if liked:
        if ctx is None or not authz.can_read_skill(ctx, skill, db=db):
            # Fail closed: no context, or no read access → never write.
            return

    bundle = ensure_liked_bundle(db, owner_id)
    existing = (
        db.query(BundleSkill)
        .filter(BundleSkill.bundle_id == bundle.id, BundleSkill.skill_id == skill.id)
        .first()
    )
    if liked and existing is None:
        db.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"))
        _touch_bundle_generation(db, bundle.id)
        db.flush()
    elif not liked and existing is not None:
        db.delete(existing)
        _touch_bundle_generation(db, bundle.id)
        db.flush()


def set_federated_like_in_bundle(
    db: Session,
    *,
    owner_id: UUID,
    federated_source: str,
    federated_slug: str,
    liked: bool,
) -> None:
    """Mirror a slug-route like of a FEDERATED skill into the deployable Liked bundle.

    spotify_2607 Phase A — L6 SUPERSESSION (plan §0b). The ponytail_0724 lock
    kept federated likes out of ``BundleSkill`` because that join drives
    ``authz.can_install`` and fleet reconcile. Decision #3 KNOWINGLY OVERRIDES
    that: 76% of the catalog is federated, and a Liked bundle that silently
    drops 3-in-4 saves is worse than useless. The override is RECORDED, not
    silent — Phase B/C ship the risk-reductions (badging, vetted/community
    install-payload split) that make it defensible.

    No authz gate: a federated like names a track we do not host, so
    ``authz.can_read_skill`` cannot apply (there is no ``Skill`` row). The
    federated identity is the trust boundary — it is a bookmark to upstream
    content, not a grant of access to hosted content. Phase B adds badging.

    Idempotent, self-healing, and transactionally staged (flush-only) so the
    caller's single commit covers both ``skill_likes`` and this mirror — the
    same atomicity contract as ``set_local_like_by_skill``.
    """
    bundle = ensure_liked_bundle(db, owner_id)
    existing = (
        db.query(BundleSkill)
        .filter(
            BundleSkill.bundle_id == bundle.id,
            BundleSkill.federated_source == federated_source,
            BundleSkill.federated_slug == federated_slug,
        )
        .first()
    )
    if liked and existing is None:
        db.add(
            BundleSkill(
                bundle_id=bundle.id,
                skill_id=None,
                federated_source=federated_source,
                federated_slug=federated_slug,
                source="custom-added",
            )
        )
        _touch_bundle_generation(db, bundle.id)
        db.flush()
    elif not liked and existing is not None:
        db.delete(existing)
        _touch_bundle_generation(db, bundle.id)
        db.flush()


def liked_library(db: Session, *, owner_id: UUID) -> dict:
    """Return the owner's typed liked shelves and the reserved follows shelf.

    ponytail_0724 R1 (Codex MUST-FIX #2 / #7 / #8). The typed ``shelves``
    payload is a FROZEN CONTRACT (``docs/briefs/liked_0711-P1.md`` §FROZEN
    CONTRACT): each entry is exactly ``{id, slug, title, liked_at}`` with a UUID
    ``id``, and the Liked bundle it serialises is a DEPLOYABLE bundle — the
    reconcile path pulls it onto the caller's agents.

    spotify_2607 Phase A — L6 supersession (plan §0b). A federated like NOW
    also lands in the deployable Liked bundle (``BundleSkill`` with
    ``skill_id=NULL`` + ``federated_source``/``federated_slug`` set). Those rows
    are surfaced on the skills shelf too, so ``"install skills from my liked
    bundle"`` works for the 76% of the catalog that is federated. They carry
    ``source`` so Phase B can badge them as community/unvetted. The frozen
    per-entry shape is preserved: a federated row's ``id`` is its surrogate
    ``BundleSkill.id`` (a real UUID, never None) and its ``slug`` is the
    federated slug — so no consumer's type assumption breaks.

    Federated likes are therefore served on their OWN additive key,
    ``federated_skills`` — new, so it breaks nothing, and structurally separate,
    so nothing can mistake a community bookmark for a deployable Liked entry.

    spotify_2607 Phase B (§0b): ``federated_skills`` entries now carry a
    ``provenance`` field (``"community"`` for federated/unvetted content,
    ``"vetted"`` for local catalog entries that passed the publish scan). This
    is the additive badging the premortem demanded — a fleet manager pulling
    the Liked bundle can tell apart vetted and community content at a glance.
    The frozen shelf shape (``{id, slug, title, liked_at}``) is unchanged.

    The legacy ``federated_skills`` top-level key is RETAINED for now (it reads
    ``skill_likes`` directly) so nothing that already consumes it breaks at the
    same moment the deployable mirror ships. It is the deletion-pass candidate
    (plan §3 Phase A step 1): once every consumer reads the deployable shelf,
    the separate key retires.
    """
    bundle = ensure_liked_bundle(db, owner_id)
    return {
        "liked_bundle_id": str(bundle.id),
        "shelves": {
            "skills": _liked_skill_shelf(db, bundle.id),
            "personalities": _liked_shelf(db, BundlePersonality, Personality, "personality_id", bundle.id),
            "loops": _liked_shelf(db, BundleCompositeLoop, CompositeLoop, "composite_loop_id", bundle.id),
        },
        # Additive (ponytail_0724). Retained through spotify_2607 Phase A as a
        # read of skill_likes; slated for retirement once the deployable shelf
        # is the single source of truth (plan §3 Phase A deletion pass).
        "federated_skills": _federated_liked_skills(db, owner_id=owner_id),
        "followed_bundles": _followed_bundles(db, owner_id),
    }


def _liked_skill_shelf(db: Session, bundle_id: UUID) -> list[dict[str, str | datetime | None]]:
    """Serialize the deployable Liked-bundle skill shelf.

    LOCAL rows (``skill_id`` set) join ``Skill`` exactly as before. FEDERATED
    rows (``skill_id`` NULL, ``federated_source``/``federated_slug`` set —
    spotify_2607 Phase A) are resolved via the hub snapshot for a human title,
    falling back to the federated slug (same fail-soft contract as
    ``_federated_liked_skills``). Both are returned in ``added_at`` order so the
    shelf stays stable across a backfill.

    The frozen per-entry shape ``{id, slug, title, liked_at}`` is preserved:
    a federated row's ``id`` is its ``BundleSkill.id`` surrogate UUID.
    """
    local_rows = (
        db.query(BundleSkill, Skill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == bundle_id, BundleSkill.skill_id.isnot(None))
        .order_by(BundleSkill.added_at.asc())
        .all()
    )
    out: list[dict[str, str | datetime | None]] = [
        {
            "id": str(artifact.id),
            "slug": artifact.slug,
            "title": artifact.title,
            "liked_at": join.added_at,
        }
        for join, artifact in local_rows
    ]

    fed_rows = (
        db.query(BundleSkill)
        .filter(
            BundleSkill.bundle_id == bundle_id,
            BundleSkill.skill_id.is_(None),
            BundleSkill.federated_source.isnot(None),
            BundleSkill.federated_slug.isnot(None),
        )
        .order_by(BundleSkill.added_at.asc())
        .all()
    )
    if fed_rows:
        hub_by_slug = resolve_federated_hub_titles(db, (r.federated_slug for r in fed_rows))
        for join in fed_rows:
            out.append(
                {
                    "id": str(join.id),
                    "slug": join.federated_slug,
                    "title": federated_title_for(hub_by_slug.get(join.federated_slug), join.federated_slug),
                    "liked_at": join.added_at,
                }
            )
    return out


def _federated_liked_skills(db: Session, *, owner_id: UUID) -> list[dict]:
    """Serialize the caller's FEDERATED skill likes (hub / skills.sh / ClawHub).

    ponytail_0724. These live in ``skill_likes`` with ``skill_id IS NULL`` and a
    ``(federated_source, federated_slug)`` identity — the only representation
    that can name a skill we do not host. They are deliberately NOT merged into
    ``shelves.skills``:

    - ``shelves.skills`` is the DEPLOYABLE Liked bundle (``BundleSkill``), which
      also drives ``authz.can_install`` and fleet reconcile. Putting a federated
      row there would grant install rights for unvetted third-party content.
    - its entry shape is a frozen contract (``{id, slug, title, liked_at}``,
      ``id`` a UUID). A federated row has no local UUID.

    Rows are newest-last (ascending ``liked_at``) to match the typed shelves'
    ordering convention, and carry ``source`` so the UI can badge them.

    TITLE RESOLUTION (ponytail_0725 — Adam: "it's in my library with a little
    changed name"). ``skill_likes`` stores only the federated IDENTITY, never
    display metadata, so the first cut fell back to ``title = federated_slug``
    and the library rendered the raw hub id
    ``skills-sh-dietrichgebert-ponytail-ponytail`` instead of ``ponytail``.
    The human title already exists in ``federation_hub_skills.title``; we now
    resolve it with ONE batched lookup (no N+1) and also surface
    ``description`` + ``origin_url`` so the card can link to the real upstream
    skill instead of a slug-guess search.

    Resolution FAILS SOFT: a like can name any ``source__slug`` pair (see
    ``_resolve_track_identity``), including a source we do not snapshot and a
    row the hub may later drop. An unresolvable row keeps the slug as its title
    — degraded, never a 500, and never a vanished like.
    """
    likes = (
        db.query(SkillLike)
        .filter(
            SkillLike.user_id == owner_id,
            SkillLike.skill_id.is_(None),
            SkillLike.federated_source.isnot(None),
            SkillLike.federated_slug.isnot(None),
        )
        .order_by(SkillLike.liked_at.asc())
        .all()
    )
    if not likes:
        return []

    # One query for every liked slug — keep this batched; a per-row lookup here
    # is an N+1 on a page that renders the user's whole library.
    hub_by_slug = resolve_federated_hub_titles(db, (like.federated_slug for like in likes))

    out: list[dict] = []
    for like in likes:
        hub = hub_by_slug.get(like.federated_slug)
        title = federated_title_for(hub, like.federated_slug)
        # spotify_2607 Phase B (§0b): provenance badging. A federated like is
        # community/unvetted by construction — we do not host it and it never
        # passed the publish scan. The trust_level column on
        # federation_hub_skills (when present) refines this, but the badge
        # vocabulary a consumer cares about is binary: did LoopSkill vet this
        # or not? Federated -> "community"; a local skill that earned its way
        # into the deployable Liked bundle -> "vetted" (never appears here).
        trust = getattr(hub, "trust_level", None) if hub is not None else None
        provenance = "vetted" if trust == "trusted" else "community"
        out.append(
            {
                "slug": like.federated_slug,
                "title": title or like.federated_slug,
                "description": (hub.description if hub is not None else None),
                "origin_url": (hub.origin_url if hub is not None else None),
                "source": like.federated_source,
                # §0b additive badge — distinguishes vetted catalog content
                # from community/unvetted federated bookmarks.
                "provenance": provenance,
                "liked_at": like.liked_at,
            }
        )
    return out


def _liked_shelf(
    db: Session,
    join_model: type,
    artifact_model: type,
    artifact_id_field: str,
    bundle_id: UUID,
) -> list[dict[str, str | datetime | None]]:
    """Serialize one ordered liked-artifact shelf without leaking join details."""
    rows = (
        db.query(join_model, artifact_model)
        .join(artifact_model, artifact_model.id == getattr(join_model, artifact_id_field))
        .filter(join_model.bundle_id == bundle_id)
        .order_by(join_model.added_at.asc())
        .all()
    )
    return [
        {
            "id": str(artifact.id),
            "slug": artifact.slug,
            "title": artifact.title,
            "liked_at": join.added_at,
        }
        for join, artifact in rows
    ]


def _followed_bundles(db: Session, owner_id: UUID) -> list[dict[str, str | datetime | None]]:
    """Serialize public bundles the caller follows without exposing their members."""
    from app.models import Bundle

    rows = (
        db.query(FollowedBundle, Bundle, User)
        .join(Bundle, Bundle.id == FollowedBundle.bundle_id)
        .outerjoin(User, User.id == Bundle.bundle_owner)
        .filter(FollowedBundle.user_id == owner_id)
        .order_by(FollowedBundle.followed_at.desc())
        .all()
    )
    return [
        {
            "id": str(bundle.id),
            "slug": bundle.slug,
            "name": bundle.name,
            "owner_handle": owner.display_name if owner is not None else None,
            "followed_at": followed.followed_at,
        }
        for followed, bundle, owner in rows
    ]
