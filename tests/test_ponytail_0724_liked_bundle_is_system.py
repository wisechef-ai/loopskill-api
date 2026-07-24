"""ponytail_0724 — the Liked bundle is a SYSTEM primitive, not a user bundle.

Two related invariants, both surfaced while wiring the "heart" control.

## 1. The Liked bundle must not appear in `owned_bundles`

`GET /api/me/library` excluded `is_base` bundles but NOT `is_liked` ones. Every
user is auto-issued a system Liked bundle (`ensure_liked_bundle`), so it leaked
into the list of bundles the user supposedly composed. Caught when the heart's
local-like path began calling `ensure_liked_bundle` on more requests —
`test_spotify1507_pha_engagement.py::test_my_library_aggregates` started seeing
2 owned bundles where it expected 1.

## 2. The free-tier bundle quota must leave room for a real bundle

`bundle_routes.py` counts EVERY owned bundle toward the tier cap, including the
auto-created Liked one. `docs/briefs/liked_0711-P5.md` flagged this as "the whole
Model-Y economics": with `free = 1`, a brand-new user already had `existing == 1`
and could never create their first real bundle.

It was resolved differently from the brief's proposal — rather than excluding
system bundles from the count, Adam raised the free cap to 2 (config/tiers.yaml,
2026-07-12): "free=2 = the auto-Liked bundle + one real editable bundle". That
is a load-bearing coupling between a YAML number and a system-bundle behaviour,
with no test pinning it. If someone later lowers free back to 1 — or excludes
system bundles from the count without lowering the cap — the tier silently
changes meaning. These tests pin the INTENT so either edit is caught.
"""

from __future__ import annotations

from uuid import uuid4

from app.liked_service import ensure_liked_bundle
from app.models import Bundle, User
from app.tier_labels import bundle_limit


def _user(db) -> User:
    handle = uuid4().hex[:8]
    u = User(id=uuid4(), email=f"{handle}@example.test", display_name=f"user-{handle}")
    db.add(u)
    db.flush()
    return u


class TestFreeTierLeavesRoomForARealBundle:
    """The cap must exceed the number of auto-created system bundles."""

    def test_free_cap_is_greater_than_one(self):
        """free == 1 is the liked_0711-P5 trap: the auto-Liked bundle fills it."""
        assert (bundle_limit("free") or 0) > 1, (
            "A free user is auto-issued a system Liked bundle which counts toward "
            "this cap (bundle_routes.py counts ALL owned bundles). free=1 means "
            "they can never create their first real bundle — see "
            "docs/briefs/liked_0711-P5.md and the rationale in config/tiers.yaml."
        )

    def test_free_cap_leaves_room_for_exactly_one_real_bundle(self, db_session):
        """Pin the documented intent: auto-Liked + one real editable bundle."""
        u = _user(db_session)
        ensure_liked_bundle(db_session, u.id)

        system_owned = (
            db_session.query(Bundle).filter(Bundle.bundle_owner == u.id, Bundle.is_liked.is_(True)).count()
        )
        assert system_owned == 1

        headroom = (bundle_limit("free") or 0) - system_owned
        assert headroom == 1, (
            f"free tier headroom is {headroom} real bundle(s); config/tiers.yaml "
            "documents it as exactly one (the auto-Liked bundle + one real "
            "editable bundle)."
        )

    def test_paid_tiers_are_not_starved_either(self):
        assert (bundle_limit("pro") or 0) > 1
        assert (bundle_limit("pro_plus") or 0) > 1

    def test_unknown_tier_falls_back_to_a_usable_free_cap(self):
        """An unknown/None tier must not strand the user at zero headroom."""
        assert (bundle_limit(None) or 0) > 1


class TestLikedBundleIsNotAnOwnedBundle:
    """`is_liked` is a system primitive — same class as `is_base`."""

    def test_ensure_liked_bundle_marks_it_as_a_system_bundle(self, db_session):
        u = _user(db_session)
        bundle = ensure_liked_bundle(db_session, u.id)
        assert bundle.is_liked is True

    def test_it_is_idempotent(self, db_session):
        """A second call must not mint a second system bundle."""
        u = _user(db_session)
        first = ensure_liked_bundle(db_session, u.id)
        second = ensure_liked_bundle(db_session, u.id)

        assert first.id == second.id
        assert (
            db_session.query(Bundle).filter(Bundle.bundle_owner == u.id, Bundle.is_liked.is_(True)).count()
            == 1
        )

    def test_owned_bundles_query_shape_excludes_system_bundles(self, db_session):
        """Mirror of the `owned` query in engagement_routes.my_library.

        A user with ONLY an auto-Liked bundle owns ZERO composed bundles.
        """
        u = _user(db_session)
        ensure_liked_bundle(db_session, u.id)

        owned = (
            db_session.query(Bundle)
            .filter(
                Bundle.bundle_owner == u.id,
                Bundle.is_base.is_(False),
                Bundle.is_liked.is_(False),
            )
            .count()
        )
        assert owned == 0, "the system Liked bundle leaked into owned_bundles"

    def test_a_real_bundle_is_still_counted_as_owned(self, db_session):
        """The exclusion must not hide genuinely user-composed bundles."""
        u = _user(db_session)
        ensure_liked_bundle(db_session, u.id)
        db_session.add(Bundle(id=uuid4(), name="My Bundle", slug="my-bundle", bundle_owner=u.id))
        db_session.flush()

        owned = (
            db_session.query(Bundle)
            .filter(
                Bundle.bundle_owner == u.id,
                Bundle.is_base.is_(False),
                Bundle.is_liked.is_(False),
            )
            .all()
        )
        assert [b.name for b in owned] == ["My Bundle"]
