"""ponytail_0724 — federated skill likes reach the library on their OWN shelf.

Adam 2026-07-24: "I want to add skill to favourite but there is no menu
similar to spotify on skills". The like backend shipped in liked_0711 but no
portal surface called it, AND a federated (hub / skills.sh / ClawHub) like was
invisible in `GET /api/library` — it lives in `skill_likes` with
`skill_id IS NULL`, while `liked_library` only read the Liked-bundle join.

Verified against prod 2026-07-24:

    POST /api/skills/skills-sh__dietrichgebert--ponytail--ponytail/like
      -> {"liked": true, "like_count": 1}          # write succeeded
    GET  /api/library
      -> {"shelves": {"skills": [], ...}}          # ...and vanished

A heart on a hub card would light up and then show an empty library — a lying
button.

## Why a SEPARATE key, not a union (R1 review, Codex REQUEST_CHANGES)

The first cut merged federated likes into `shelves.skills` with `id: None`.
Codex R1 flagged that as two MUST-FIXes and it was right:

1. `shelves.*` entry shape is a FROZEN CONTRACT — exactly
   `{id, slug, title, liked_at}` with a UUID `id`
   (`docs/briefs/liked_0711-P1.md` §FROZEN CONTRACT, committed in c4f5612).
   Emitting `id: None` silently changes the field's type for every consumer.
2. The Liked bundle is DEPLOYABLE — that same brief's acceptance criteria say a
   reconcile pull carries its contents onto the caller's agents, and
   `BundleSkill` also drives `authz.can_install`. A federated row in that shelf
   implies an agent can deploy an artifact we neither host nor vet.

So federated likes get an additive top-level key, `federated_skills`. New key =
no consumer breaks; separate structure = a community bookmark can never be
mistaken for a deployable Liked entry.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.liked_service import ensure_liked_bundle
from app.library_service import liked_library
from app.models import BundleSkill, SkillLike, User
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


def _bundle_skill(db, owner_id, skill) -> None:
    bundle = ensure_liked_bundle(db, owner_id)
    db.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"))
    db.flush()


class TestFederatedLikesReachTheLibrary:
    """A hearted hub skill must be visible — on `federated_skills`."""

    def test_federated_like_is_returned(self, db_session):
        u = _user(db_session)
        _federated_like(db_session, u.id, "skills-sh", "dietrichgebert--ponytail--ponytail")

        shelf = liked_library(db_session, owner_id=u.id)["federated_skills"]

        assert len(shelf) == 1, "the federated like vanished — the lying-button bug"
        assert shelf[0]["slug"] == "dietrichgebert--ponytail--ponytail"

    def test_federated_entry_carries_its_source_for_badging(self, db_session):
        u = _user(db_session)
        _federated_like(db_session, u.id, "skills-sh", "owner--repo--skill")

        row = liked_library(db_session, owner_id=u.id)["federated_skills"][0]

        assert row["source"] == "skills-sh"
        assert row["liked_at"] is not None

    def test_key_is_present_even_when_empty(self, db_session):
        """A stable key — consumers must not have to feature-detect it."""
        u = _user(db_session)
        assert liked_library(db_session, owner_id=u.id)["federated_skills"] == []

    def test_another_users_federated_like_is_not_leaked(self, db_session):
        mine = _user(db_session)
        theirs = _user(db_session)
        _federated_like(db_session, theirs.id, "skills-sh", "not--yours--skill")

        assert liked_library(db_session, owner_id=mine.id)["federated_skills"] == []

    def test_ordering_is_ascending_by_liked_at(self, db_session):
        u = _user(db_session)
        _federated_like(db_session, u.id, "skills-sh", "first")
        _federated_like(db_session, u.id, "clawhub", "second")

        slugs = [r["slug"] for r in liked_library(db_session, owner_id=u.id)["federated_skills"]]

        assert slugs == ["first", "second"]

    @pytest.mark.parametrize("source", ["skills-sh", "clawhub", "lobehub", "github-oss"])
    def test_every_federation_source_is_representable(self, db_session, source):
        u = _user(db_session)
        _federated_like(db_session, u.id, source, f"owner--repo--{source}")

        assert liked_library(db_session, owner_id=u.id)["federated_skills"][0]["source"] == source


class TestFrozenContractIsNotBroken:
    """R1 MUST-FIX #2/#7/#8 — the typed shelves keep their pinned shape."""

    def test_typed_shelves_keep_the_exact_four_keys(self, db_session):
        u = _user(db_session)
        skill = make_skill(db_session, slug="local-one", title="Local One")
        _bundle_skill(db_session, u.id, skill)
        _federated_like(db_session, u.id, "skills-sh", "fed--one")

        shelf = liked_library(db_session, owner_id=u.id)["shelves"]["skills"]

        assert len(shelf) == 1
        assert set(shelf[0]) == {"id", "slug", "title", "liked_at"}

    def test_typed_shelf_id_is_always_a_uuid_string_never_none(self, db_session):
        """R1 MUST-FIX #8: `id` must not silently become nullable."""
        u = _user(db_session)
        skill = make_skill(db_session, slug="local-two", title="Local Two")
        _bundle_skill(db_session, u.id, skill)
        _federated_like(db_session, u.id, "clawhub", "fed--two")

        for row in liked_library(db_session, owner_id=u.id)["shelves"]["skills"]:
            assert isinstance(row["id"], str)

    def test_federated_rows_never_enter_the_deployable_shelf(self, db_session):
        """The load-bearing safety property: no unvetted artifact in the
        deployable Liked bundle (it drives authz.can_install + reconcile)."""
        u = _user(db_session)
        for i in range(3):
            _federated_like(db_session, u.id, "skills-sh", f"fed--{i}")

        out = liked_library(db_session, owner_id=u.id)

        assert out["shelves"]["skills"] == []
        assert len(out["federated_skills"]) == 3

    def test_no_bundleskill_row_is_created_for_a_federated_like(self, db_session):
        """Reading the library must never MUTATE the deployable bundle."""
        u = _user(db_session)
        _federated_like(db_session, u.id, "skills-sh", "fed--nowrite")
        bundle = ensure_liked_bundle(db_session, u.id)

        liked_library(db_session, owner_id=u.id)

        assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == bundle.id).count() == 0

    def test_response_carries_no_count_or_total_field(self, db_session):
        """The P1 brief's hard rule survives the new key."""
        u = _user(db_session)
        _federated_like(db_session, u.id, "skills-sh", "fed--x")

        body = str(liked_library(db_session, owner_id=u.id)).lower()

        assert "count" not in body
        assert "total" not in body
