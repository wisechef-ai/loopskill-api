"""ponytail_0724 — federated skills must appear in the Liked library.

BUG (found 2026-07-24, Adam: "no menu similar to spotify on skills"):

LoopSkill has TWO disjoint like systems that never talk to each other.

1. ``engagement_routes.py`` writes ``skill_likes`` — the only table that can
   represent a FEDERATED track (``federated_source`` + ``federated_slug``,
   ``skill_id`` NULL). This is what a hub / skills.sh card can be hearted into.
2. ``library_service.liked_library`` reads the Liked *bundle* join tables and
   INNER JOINs a local ``Skill`` row.

A federated skill has no local ``Skill`` row, so the inner join drops it. Verified
against prod 2026-07-24:

    POST /api/skills/skills-sh__dietrichgebert--ponytail--ponytail/like
      -> {"liked": true, "like_count": 1}          # write succeeded
    GET  /api/library
      -> {"shelves": {"skills": [], ...}}          # ...and vanished

A heart button on a hub card would light up and then show an empty library —
a lying button. Adam's decision (2026-07-24): UNIFY ON READ. ``liked_library``
returns local Liked-bundle skills UNION federated ``skill_likes``. No schema
change, no ``BundleSkill`` writes for federated rows (that join drives
``authz.can_install`` + fleet reconcile — polluting it would grant install
rights to skills we do not host).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.library_service import liked_library
from app.liked_service import ensure_liked_bundle
from app.models import SkillLike, User
from tests.conftest import make_skill


def _user(db) -> User:
    handle = uuid4().hex[:8]
    u = User(id=uuid4(), email=f"{handle}@example.test", display_name=f"user-{handle}")
    db.add(u)
    db.flush()
    return u


def _federated_like(db, user_id, source: str, slug: str) -> SkillLike:
    like = SkillLike(
        id=uuid4(),
        user_id=user_id,
        skill_id=None,
        federated_source=source,
        federated_slug=slug,
    )
    db.add(like)
    db.flush()
    return like


class TestFederatedLikesReachTheLibrary:
    """The union read: a hearted hub skill must show up on the skills shelf."""

    def test_federated_like_appears_on_the_skills_shelf(self, db_session):
        u = _user(db_session)
        _federated_like(db_session, u.id, "skills-sh", "dietrichgebert--ponytail--ponytail")

        shelf = liked_library(db_session, owner_id=u.id)["shelves"]["skills"]

        assert len(shelf) == 1, "the federated like vanished — the lying-button bug"
        assert shelf[0]["slug"] == "dietrichgebert--ponytail--ponytail"

    def test_federated_entry_is_labelled_with_its_source(self, db_session):
        """The UI must be able to badge the row as external, not local."""
        u = _user(db_session)
        _federated_like(db_session, u.id, "skills-sh", "owner--repo--skill")

        row = liked_library(db_session, owner_id=u.id)["shelves"]["skills"][0]

        assert row["source"] == "skills-sh"
        assert row["federated"] is True

    def test_local_skills_are_labelled_local(self, db_session):
        u = _user(db_session)
        skill = make_skill(db_session, slug="local-one", title="Local One")
        bundle = ensure_liked_bundle(db_session, u.id)
        from app.models import BundleSkill

        db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"))
        db_session.flush()

        row = liked_library(db_session, owner_id=u.id)["shelves"]["skills"][0]

        assert row["source"] == "local"
        assert row["federated"] is False
        assert row["id"] == str(skill.id)

    def test_local_and_federated_coexist_on_one_shelf(self, db_session):
        u = _user(db_session)
        skill = make_skill(db_session, slug="local-two", title="Local Two")
        bundle = ensure_liked_bundle(db_session, u.id)
        from app.models import BundleSkill

        db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"))
        _federated_like(db_session, u.id, "skills-sh", "owner--repo--fed")
        db_session.flush()

        shelf = liked_library(db_session, owner_id=u.id)["shelves"]["skills"]

        assert len(shelf) == 2
        assert {r["source"] for r in shelf} == {"local", "skills-sh"}

    def test_federated_row_has_no_local_uuid_id(self, db_session):
        """A federated row must not fake a local artifact UUID."""
        u = _user(db_session)
        _federated_like(db_session, u.id, "clawhub", "api-gateway")

        row = liked_library(db_session, owner_id=u.id)["shelves"]["skills"][0]

        assert row["id"] is None, "federated rows must not claim a local artifact id"

    def test_another_users_federated_like_is_not_leaked(self, db_session):
        """Per-user isolation — the union must not widen the blast radius."""
        mine = _user(db_session)
        theirs = _user(db_session)
        _federated_like(db_session, theirs.id, "skills-sh", "not--yours--skill")

        shelf = liked_library(db_session, owner_id=mine.id)["shelves"]["skills"]

        assert shelf == []

    def test_local_like_recorded_in_skill_likes_is_not_double_counted(self, db_session):
        """A LOCAL row in skill_likes must not duplicate the bundle entry."""
        u = _user(db_session)
        skill = make_skill(db_session, slug="dedupe-me", title="Dedupe Me")
        bundle = ensure_liked_bundle(db_session, u.id)
        from app.models import BundleSkill

        db_session.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"))
        db_session.add(SkillLike(id=uuid4(), user_id=u.id, skill_id=skill.id))
        db_session.flush()

        shelf = liked_library(db_session, owner_id=u.id)["shelves"]["skills"]

        assert len(shelf) == 1, "local skill double-counted across both systems"

    def test_shelf_still_carries_liked_at(self, db_session):
        """Contract preserved: every shelf row keeps its liked_at ordering key."""
        u = _user(db_session)
        _federated_like(db_session, u.id, "skills-sh", "owner--repo--ts")

        row = liked_library(db_session, owner_id=u.id)["shelves"]["skills"][0]

        assert row["liked_at"] is not None

    def test_empty_library_is_still_empty(self, db_session):
        u = _user(db_session)
        assert liked_library(db_session, owner_id=u.id)["shelves"]["skills"] == []

    @pytest.mark.parametrize("source", ["skills-sh", "clawhub", "lobehub", "github-oss"])
    def test_every_federation_source_is_representable(self, db_session, source):
        u = _user(db_session)
        _federated_like(db_session, u.id, source, f"owner--repo--{source}")

        row = liked_library(db_session, owner_id=u.id)["shelves"]["skills"][0]

        assert row["source"] == source
        assert row["federated"] is True
