"""ponytail_0725 — a federated like shows its HUMAN title, not the raw hub id.

Adam 2026-07-25, after the heart shipped: "for the skill i have added to liked
— ponytail — it's in my library with little changed name; the name changed from
ponytail to `skills-sh-dietrichgebert-ponytail-ponytail`".

Root cause: `skill_likes` stores only the federated IDENTITY
(`federated_source` + `federated_slug`), never display metadata. The first cut
of `_federated_liked_skills` therefore fell back to::

    "title": like.federated_slug          # the raw hub id

...so the library card rendered the machine slug. The portal was innocent — it
already renders `row.title`; the API was handing it a slug and calling it a
title.

The human title was in the database the whole time. Verified in prod:

    select slug, title, origin_url from federation_hub_skills
     where slug = 'skills-sh-dietrichgebert-ponytail-ponytail';

    slug       | skills-sh-dietrichgebert-ponytail-ponytail
    title      | ponytail
    origin_url | https://github.com/dietrichgebert/ponytail/tree/main/skills/ponytail

So the fix is a resolution join against `federation_hub_skills`, batched (one
query for all liked slugs — this renders a whole library, an N+1 here is a real
regression), and FAILING SOFT: `_resolve_track_identity` accepts ANY
`source__slug` pair, so a like can legitimately name a source we do not
snapshot, and the hub snapshot can drop a row between the like and the read.
An unresolvable row must degrade to its slug — never 500, never disappear.

`origin_url` is surfaced alongside so the library card can deep-link to the
real upstream skill instead of guessing at a `/browse?q=<slug>` search.
"""

from __future__ import annotations

from uuid import uuid4

from app.library_service import liked_library
from app.models import FederationHubSkill, SkillLike, User

# The exact row Adam liked, as it exists in the prod hub snapshot.
PONYTAIL_SLUG = "skills-sh-dietrichgebert-ponytail-ponytail"
PONYTAIL_TITLE = "ponytail"
PONYTAIL_ORIGIN = "https://github.com/dietrichgebert/ponytail/tree/main/skills/ponytail"


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


def _hub_row(db, *, slug: str, title: str, origin_url: str | None = None, description=None):
    row = FederationHubSkill(
        slug=slug,
        title=title,
        description=description,
        source="hermes-hub",
        origin_url=origin_url,
    )
    db.add(row)
    db.flush()
    return row


def _only_federated(db, owner_id) -> dict:
    rows = liked_library(db, owner_id=owner_id)["federated_skills"]
    assert len(rows) == 1, f"expected exactly one federated like, got {rows}"
    return rows[0]


class TestFederatedLikeResolvesItsTitle:
    """The reported bug, pinned exactly."""

    def test_title_is_the_human_name_not_the_slug(self, db_session):
        u = _user(db_session)
        _hub_row(
            db_session,
            slug=PONYTAIL_SLUG,
            title=PONYTAIL_TITLE,
            origin_url=PONYTAIL_ORIGIN,
        )
        _federated_like(db_session, u.id, "hermes-hub", PONYTAIL_SLUG)

        row = _only_federated(db_session, u.id)

        # THE regression: this is what Adam saw in his library.
        assert row["title"] != PONYTAIL_SLUG
        assert row["title"] == PONYTAIL_TITLE

    def test_slug_is_still_the_stable_identity(self, db_session):
        """Resolving the title must not change the like's identity.

        `slug` is what the heart posts back to unlike, and what the UI keys on.
        A 'fix' that replaced slug with the title would break unliking.
        """
        u = _user(db_session)
        _hub_row(db_session, slug=PONYTAIL_SLUG, title=PONYTAIL_TITLE)
        _federated_like(db_session, u.id, "hermes-hub", PONYTAIL_SLUG)

        row = _only_federated(db_session, u.id)

        assert row["slug"] == PONYTAIL_SLUG
        assert row["source"] == "hermes-hub"

    def test_origin_url_is_surfaced_for_deep_linking(self, db_session):
        u = _user(db_session)
        _hub_row(
            db_session,
            slug=PONYTAIL_SLUG,
            title=PONYTAIL_TITLE,
            origin_url=PONYTAIL_ORIGIN,
        )
        _federated_like(db_session, u.id, "hermes-hub", PONYTAIL_SLUG)

        assert _only_federated(db_session, u.id)["origin_url"] == PONYTAIL_ORIGIN

    def test_description_is_surfaced(self, db_session):
        u = _user(db_session)
        _hub_row(
            db_session,
            slug=PONYTAIL_SLUG,
            title=PONYTAIL_TITLE,
            description="Indexed by skills.sh from dietrichgebert/ponytail",
        )
        _federated_like(db_session, u.id, "hermes-hub", PONYTAIL_SLUG)

        row = _only_federated(db_session, u.id)
        assert row["description"] == "Indexed by skills.sh from dietrichgebert/ponytail"


class TestResolutionFailsSoft:
    """A like must NEVER vanish or 500 because the hub cannot resolve it."""

    def test_unknown_slug_degrades_to_the_slug(self, db_session):
        """No hub row at all — e.g. a source we do not snapshot."""
        u = _user(db_session)
        _federated_like(db_session, u.id, "clawhub", "some--unindexed--skill")

        row = _only_federated(db_session, u.id)

        assert row["slug"] == "some--unindexed--skill"
        assert row["title"] == "some--unindexed--skill"  # degraded, still present
        assert row["origin_url"] is None
        assert row["description"] is None

    def test_blank_hub_title_degrades_to_the_slug(self, db_session):
        """`title` is NOT NULL but defaults to '' — a blank is unresolved."""
        u = _user(db_session)
        _hub_row(db_session, slug=PONYTAIL_SLUG, title="")
        _federated_like(db_session, u.id, "hermes-hub", PONYTAIL_SLUG)

        assert _only_federated(db_session, u.id)["title"] == PONYTAIL_SLUG

    def test_whitespace_hub_title_degrades_to_the_slug(self, db_session):
        u = _user(db_session)
        _hub_row(db_session, slug=PONYTAIL_SLUG, title="   ")
        _federated_like(db_session, u.id, "hermes-hub", PONYTAIL_SLUG)

        assert _only_federated(db_session, u.id)["title"] == PONYTAIL_SLUG

    def test_no_likes_returns_empty_without_querying(self, db_session):
        u = _user(db_session)
        assert liked_library(db_session, owner_id=u.id)["federated_skills"] == []


class TestResolutionIsCorrectAcrossRows:
    """Batched resolution must not mismatch titles between rows."""

    def test_each_like_gets_its_own_title(self, db_session):
        """The ponytail repo publishes several sibling skills with similar slugs.

        A sloppy prefix/`LIKE` match would cross-assign these.
        """
        u = _user(db_session)
        siblings = {
            "skills-sh-dietrichgebert-ponytail-ponytail": "ponytail",
            "skills-sh-dietrichgebert-ponytail-ponytail-audit": "ponytail-audit",
            "skills-sh-dietrichgebert-ponytail-ponytail-debt": "ponytail-debt",
        }
        for slug, title in siblings.items():
            _hub_row(db_session, slug=slug, title=title)
            _federated_like(db_session, u.id, "hermes-hub", slug)

        rows = liked_library(db_session, owner_id=u.id)["federated_skills"]

        assert {r["slug"]: r["title"] for r in rows} == siblings

    def test_resolved_and_unresolved_coexist(self, db_session):
        """One good row must not suppress a degraded one, or vice versa."""
        u = _user(db_session)
        _hub_row(db_session, slug=PONYTAIL_SLUG, title=PONYTAIL_TITLE)
        _federated_like(db_session, u.id, "hermes-hub", PONYTAIL_SLUG)
        _federated_like(db_session, u.id, "clawhub", "ghost--skill")

        rows = liked_library(db_session, owner_id=u.id)["federated_skills"]

        assert {r["title"] for r in rows} == {PONYTAIL_TITLE, "ghost--skill"}

    def test_resolution_is_per_user(self, db_session):
        """Another user's identical like must not leak into this library."""
        mine = _user(db_session)
        theirs = _user(db_session)
        _hub_row(db_session, slug=PONYTAIL_SLUG, title=PONYTAIL_TITLE)
        _federated_like(db_session, mine.id, "hermes-hub", PONYTAIL_SLUG)
        _federated_like(db_session, theirs.id, "hermes-hub", PONYTAIL_SLUG)

        assert len(liked_library(db_session, owner_id=mine.id)["federated_skills"]) == 1

    def test_resolution_is_batched_not_n_plus_1(self, db_session):
        """Guard the hot path: one hub query regardless of how many likes.

        Counts SELECTs against federation_hub_skills via SQLAlchemy events. A
        per-row lookup here would scale with the size of a user's library.
        """
        from sqlalchemy import event

        u = _user(db_session)
        for i in range(8):
            slug = f"skills-sh-fixture-skill-{i}"
            _hub_row(db_session, slug=slug, title=f"skill-{i}")
            _federated_like(db_session, u.id, "hermes-hub", slug)

        hub_selects = []

        def _count(conn, cursor, statement, params, context, executemany):
            if "federation_hub_skills" in statement and statement.lstrip().upper().startswith("SELECT"):
                hub_selects.append(statement)

        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", _count)
        try:
            rows = liked_library(db_session, owner_id=u.id)["federated_skills"]
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        assert len(rows) == 8
        assert len(hub_selects) == 1, (
            f"expected ONE batched hub lookup, got {len(hub_selects)} — N+1 regression"
        )


class TestFrozenContractIsUntouched:
    """The additive fix must not disturb the pinned `shelves` shape."""

    def test_shelves_keys_unchanged(self, db_session):
        u = _user(db_session)
        _hub_row(db_session, slug=PONYTAIL_SLUG, title=PONYTAIL_TITLE)
        _federated_like(db_session, u.id, "hermes-hub", PONYTAIL_SLUG)

        out = liked_library(db_session, owner_id=u.id)

        assert set(out["shelves"].keys()) == {"skills", "personalities", "loops"}
        # A federated bookmark must never enter the DEPLOYABLE liked shelf.
        assert out["shelves"]["skills"] == []
        # liked_0711-P1: no count/total field anywhere in this body.
        assert "count" not in out and "total" not in out
