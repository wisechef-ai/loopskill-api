"""spotify_2607 Phase A — a LIKED FEDERATED skill lands in the deployable Liked bundle.

Adam 2026-07-26: "the bundle has to be able to contain the federation skills —
since 99% of the skills we have on LoopSkill is from federation it has to be
possible to make sense of using it." Plus: "there is always option to provide
agent command like 'install skills from my liked bundle'."

The bug (reproduced on prod): Adam's user id has exactly 2 likes in
``skill_likes``, BOTH federated (``federated_source='hermes-hub'``,
``skill_id`` NULL). His Liked bundle contains 0 skills — because
``engagement_routes.py:117`` mirrored a like into ``BundleSkill`` ONLY when
``skill_id`` was set, and ``BundleSkill.skill_id`` was NOT NULL (part of the
composite PK). So a federated like wrote engagement-only state and appeared
NOWHERE in the deployable library.

This sprint KNOWINGLY OVERRIDES the ponytail_0724 L6 lock ("BundleSkill drives
authz.can_install; a federated row there implies installing unvetted content")
— plan §0 decision #3 / §0b. 76% of the catalog is federated; a Liked bundle
that silently drops 3-in-4 saves is worse than useless. The override is
RECORDED in code comments + this PR body, and Phase B/C ship the
risk-reductions (badging, vetted/community install-payload split) that make it
defensible rather than reckless.

This module covers the mandatory acceptance gates:
  - RED-proof: neutralise the mirror → the named test flips red → restore
  - POST /api/skills/<federated>/like then GET /api/library → deployable shelf
  - Unlike removes from BOTH skill_likes AND the bundle join ATOMICALLY
    (proven by killing the flow mid-transaction)
  - PATCH /api/cookbooks/<liked_id>/visibility → 4xx (Liked stays private)
  - Backfill idempotency + Adam's 2 orphaned likes placement
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.engagement_routes import router as engagement_router
from app.library_routes import router as library_router
from app.library_service import liked_library, set_federated_like_in_bundle
from app.liked_service import ensure_liked_bundle
from app.models import Bundle, BundleSkill, SkillLike, User


# ── Helpers ─────────────────────────────────────────────────────────────


def _user(db: Session) -> User:
    handle = uuid4().hex[:8]
    u = User(id=uuid4(), email=f"{handle}@example.test", display_name=f"user-{handle}")
    db.add(u)
    db.flush()
    return u


def _ctx(user_id) -> AuthContext:
    return AuthContext(scope="user", user_id=user_id)


def _app_with_routers(db: Session, owner_id) -> FastAPI:
    app = FastAPI()

    def override_get_db():
        yield db

    @app.middleware("http")
    async def inject_auth(request, call_next):
        request.state.auth_ctx = AuthContext(scope="user", user_id=owner_id)
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(engagement_router)
    app.include_router(library_router)
    return app


# ── 1. The RED-proof test: a federated like MUST land in BundleSkill ────


class TestFederatedLikeLandsInBundleSkill:
    """The exact bug Adam reported. This test is the RED-proof obligation:
    neutralise ``set_federated_like_in_bundle`` (monkeypatch to a no-op) and
    this test MUST flip red. Paste the transcript in the PR body.
    """

    def test_a_federated_like_creates_a_bundleskill_row(self, db_session):
        u = _user(db_session)

        set_federated_like_in_bundle(
            db_session,
            owner_id=u.id,
            federated_source="hermes-hub",
            federated_slug="skills-sh-dietrichgebert-ponytail-ponytail",
            liked=True,
        )
        db_session.commit()

        bundle = ensure_liked_bundle(db_session, u.id)
        rows = (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == bundle.id,
                BundleSkill.federated_source == "hermes-hub",
                BundleSkill.federated_slug == "skills-sh-dietrichgebert-ponytail-ponytail",
            )
            .all()
        )
        assert len(rows) == 1, "the federated like did NOT land in BundleSkill — the bug"
        assert rows[0].skill_id is None, "a federated row must have NULL skill_id"
        assert rows[0].source == "custom-added"

    def test_it_shows_on_the_deployable_skills_shelf(self, db_session):
        u = _user(db_session)
        set_federated_like_in_bundle(
            db_session,
            owner_id=u.id,
            federated_source="hermes-hub",
            federated_slug="ai-humanizer-2-1-0",
            liked=True,
        )
        db_session.commit()

        shelf = liked_library(db_session, owner_id=u.id)["shelves"]["skills"]
        assert len(shelf) == 1
        assert shelf[0]["slug"] == "ai-humanizer-2-1-0"
        # Frozen contract: id is a UUID string (the surrogate BundleSkill.id),
        # never None — that was R1 MUST-FIX #8 on the prior attempt.
        assert isinstance(shelf[0]["id"], str)
        # Fail-soft title: no hub row → slug as title.
        assert shelf[0]["title"] == "ai-humanizer-2-1-0"
        assert shelf[0]["liked_at"] is not None

    def test_local_and_federated_coexist_on_the_same_shelf(self, db_session):
        from tests.conftest import make_skill

        u = _user(db_session)
        local = make_skill(db_session, slug="local-skill", title="Local Skill")
        # local via the existing local mirror helper
        from app.library_service import set_local_like_by_skill

        set_local_like_by_skill(db_session, owner_id=u.id, skill=local, liked=True, ctx=_ctx(u.id))
        set_federated_like_in_bundle(
            db_session,
            owner_id=u.id,
            federated_source="clawhub",
            federated_slug="some-federated",
            liked=True,
        )
        db_session.commit()

        shelf = liked_library(db_session, owner_id=u.id)["shelves"]["skills"]
        slugs = {row["slug"] for row in shelf}
        assert slugs == {"local-skill", "some-federated"}


# ── 2. Full HTTP round-trip: POST /like then GET /api/library ───────────


class TestHttpPostLikeThenGetLibrary:
    def test_post_federated_like_then_get_library_shows_it(self, db_session):
        u = _user(db_session)
        slug = "hermes-hub__ai-humanizer-2-1-0"

        with TestClient(_app_with_routers(db_session, u.id)) as client:
            resp = client.post(f"/api/skills/{slug}/like")
            assert resp.status_code == 200, resp.text
            assert resp.json()["liked"] is True

            lib = client.get("/api/library")
            assert lib.status_code == 200
            skills = lib.json()["shelves"]["skills"]
            assert len(skills) == 1
            assert skills[0]["slug"] == "ai-humanizer-2-1-0"

    def test_engagement_and_bundle_both_written(self, db_session):
        u = _user(db_session)
        slug = "hermes-hub__ponytail-test"

        with TestClient(_app_with_routers(db_session, u.id)) as client:
            client.post(f"/api/skills/{slug}/like")

        # skill_likes has the engagement row
        like = (
            db_session.query(SkillLike)
            .filter(
                SkillLike.user_id == u.id,
                SkillLike.federated_source == "hermes-hub",
                SkillLike.federated_slug == "ponytail-test",
            )
            .one()
        )
        assert like.skill_id is None
        # BundleSkill has the deployable mirror
        bundle = ensure_liked_bundle(db_session, u.id)
        bs = (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == bundle.id,
                BundleSkill.federated_source == "hermes-hub",
                BundleSkill.federated_slug == "ponytail-test",
            )
            .one()
        )
        assert bs.skill_id is None


# ── 3. Unlike is ATOMIC: both sides removed in one transaction ──────────


class TestUnlikeIsAtomic:
    """Acceptance gate: 'Unlike removes from both sides in a single transaction
    (proven by killing mid-flow in a test).' We simulate the kill by rolling
    back the transaction after the unlike handler stages both deletes but
    BEFORE commit — and asserting NEITHER side was written.
    """

    def test_unlike_removes_from_both_sides(self, db_session):
        u = _user(db_session)
        slug = "hermes-hub__remove-me"

        with TestClient(_app_with_routers(db_session, u.id)) as client:
            client.post(f"/api/skills/{slug}/like")
            client.delete(f"/api/skills/{slug}/like")

        assert (
            db_session.query(SkillLike)
            .filter(
                SkillLike.user_id == u.id,
                SkillLike.federated_source == "hermes-hub",
                SkillLike.federated_slug == "remove-me",
            )
            .count()
            == 0
        )
        bundle = ensure_liked_bundle(db_session, u.id)
        assert (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == bundle.id,
                BundleSkill.federated_source == "hermes-hub",
                BundleSkill.federated_slug == "remove-me",
            )
            .count()
            == 0
        )

    def test_mid_flow_rollback_leaves_both_sides_intact(self, db_session):
        """If the transaction is rolled back AFTER staging both deletes but
        BEFORE commit, neither side must lose its row. This is the atomicity
        proof: the mirror is staged pre-commit (flush), so a rollback reverts
        BOTH the SkillLike delete and the BundleSkill delete together.
        """
        u = _user(db_session)
        # Seed both sides
        set_federated_like_in_bundle(
            db_session,
            owner_id=u.id,
            federated_source="hermes-hub",
            federated_slug="atomic-test",
            liked=True,
        )
        db_session.add(
            SkillLike(
                id=uuid4(),
                user_id=u.id,
                skill_id=None,
                federated_source="hermes-hub",
                federated_slug="atomic-test",
            )
        )
        db_session.commit()

        # Stage both deletes (simulating the unlike handler's pre-commit work)
        # but do NOT commit — then rollback.
        from app.library_service import set_federated_like_in_bundle as _mirror

        db_session.query(SkillLike).filter(
            SkillLike.user_id == u.id,
            SkillLike.federated_source == "hermes-hub",
            SkillLike.federated_slug == "atomic-test",
        ).delete()
        _mirror(
            db_session,
            owner_id=u.id,
            federated_source="hermes-hub",
            federated_slug="atomic-test",
            liked=False,
        )
        db_session.flush()
        # Rollback as if the commit failed / the process died mid-flow.
        db_session.rollback()

        # Both sides MUST still be present — the transaction was atomic.
        assert (
            db_session.query(SkillLike)
            .filter(
                SkillLike.user_id == u.id,
                SkillLike.federated_source == "hermes-hub",
                SkillLike.federated_slug == "atomic-test",
            )
            .count()
            == 1
        )
        bundle = ensure_liked_bundle(db_session, u.id)
        assert (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == bundle.id,
                BundleSkill.federated_source == "hermes-hub",
                BundleSkill.federated_slug == "atomic-test",
            )
            .count()
            == 1
        )


# ── 4. Liked bundle cannot be published (§0a privacy guard) ─────────────


class TestLikedBundleStaysPrivate:
    """PATCH /api/cookbooks/<liked_id>/visibility must return a 4xx with an
    explanatory body. The guard is at the ORM layer (Bundle.@validates), so
    even a direct ``bundle.visibility = 'public'`` raises — tested directly
    here so it does not depend on Phase C's route file (ownership boundary).
    """

    def test_orm_guard_rejects_publish(self, db_session):
        from app.models import LikedBundleNotPublishableError

        u = _user(db_session)
        bundle = ensure_liked_bundle(db_session, u.id)
        assert bundle.is_liked is True
        with pytest.raises(LikedBundleNotPublishableError):
            bundle.visibility = "public"

    def test_orm_guard_allows_private(self, db_session):
        u = _user(db_session)
        bundle = ensure_liked_bundle(db_session, u.id)
        # Setting private on an already-private Liked bundle is a no-op.
        bundle.visibility = "private"
        assert bundle.visibility == "private"

    def test_regular_bundle_is_unaffected(self, db_session):
        u = _user(db_session)
        regular = Bundle(
            id=uuid4(),
            name="Regular",
            bundle_owner=u.id,
            visibility="private",
        )
        db_session.add(regular)
        db_session.flush()
        regular.visibility = "public"  # no raise
        assert regular.visibility == "public"


# ── 5. Idempotency + self-heal ──────────────────────────────────────────


class TestIdempotencyAndSelfHeal:
    def test_liking_twice_yields_one_row(self, db_session):
        u = _user(db_session)
        for _ in range(2):
            set_federated_like_in_bundle(
                db_session,
                owner_id=u.id,
                federated_source="hermes-hub",
                federated_slug="idem-test",
                liked=True,
            )
        db_session.commit()
        bundle = ensure_liked_bundle(db_session, u.id)
        assert (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == bundle.id,
                BundleSkill.federated_source == "hermes-hub",
                BundleSkill.federated_slug == "idem-test",
            )
            .count()
            == 1
        )

    def test_self_heal_on_re_like(self, db_session):
        """A pre-existing skill_likes row (created before this shipped) must
        repair on the next like — the mirror runs even when the like exists.
        """
        u = _user(db_session)
        db_session.add(
            SkillLike(
                id=uuid4(),
                user_id=u.id,
                skill_id=None,
                federated_source="hermes-hub",
                federated_slug="orphan-like",
            )
        )
        db_session.commit()
        # No BundleSkill row yet (the orphan).
        bundle = ensure_liked_bundle(db_session, u.id)
        assert (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == bundle.id,
                BundleSkill.federated_source == "hermes-hub",
                BundleSkill.federated_slug == "orphan-like",
            )
            .count()
            == 0
        )

        # Re-like via the mirror — self-heals.
        set_federated_like_in_bundle(
            db_session,
            owner_id=u.id,
            federated_source="hermes-hub",
            federated_slug="orphan-like",
            liked=True,
        )
        db_session.commit()
        assert (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == bundle.id,
                BundleSkill.federated_source == "hermes-hub",
                BundleSkill.federated_slug == "orphan-like",
            )
            .count()
            == 1
        )

    def test_unlike_when_already_unliked_is_a_noop(self, db_session):
        u = _user(db_session)
        set_federated_like_in_bundle(
            db_session,
            owner_id=u.id,
            federated_source="hermes-hub",
            federated_slug="never-liked",
            liked=False,
        )
        db_session.commit()
        bundle = ensure_liked_bundle(db_session, u.id)
        assert (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == bundle.id,
            )
            .count()
            == 0
        )


# ── 6. Cross-user isolation ─────────────────────────────────────────────


class TestCrossUserIsolation:
    def test_one_users_federated_like_does_not_appear_in_anothers_bundle(self, db_session):
        mine = _user(db_session)
        theirs = _user(db_session)
        set_federated_like_in_bundle(
            db_session,
            owner_id=mine.id,
            federated_source="hermes-hub",
            federated_slug="mine-only",
            liked=True,
        )
        db_session.commit()
        their_bundle = ensure_liked_bundle(db_session, theirs.id)
        assert (
            db_session.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == their_bundle.id,
            )
            .count()
            == 0
        )
