"""autopilot_0308 M2 — public tier ladder = exactly 3 (Free / Pro / Enterprise).

D-003: pro_plus disappears from public presentation. D-010: the 5 live
pro_plus users migrate to pro (script only — see scripts/migrate_pro_plus_to_pro.py).

Premortem risk #2 (L4 x I10): pro_plus must NOT be dropped from the data
layer. It stays a fully valid `db_slug` — for the migration window (5 users
still on it) and for Enterprise contracts afterwards (hub D-005: "anything
above Pro is a sales conversation, not an automated meter"). These tests
pin that invariant so a future cleanup pass can't "tidy up" a live enum
value and cause the data-loss migration the premortem warns about.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import User


class TestTiersYamlPublicFlag:
    def test_pro_plus_marked_not_public(self):
        from app.tier_labels import _tiers

        tiers = _tiers()
        assert tiers["pro_plus"].get("public") is False

    def test_free_and_pro_remain_public(self):
        from app.tier_labels import _tiers

        tiers = _tiers()
        # Absence of the `public` key means "public" (default) — free/pro
        # never opted out.
        assert tiers["free"].get("public", True) is True
        assert tiers["pro"].get("public", True) is True

    def test_pro_plus_db_slug_and_price_untouched(self):
        """The db_slug and price stay intact — only presentation changes."""
        from app.tier_labels import _tiers

        tiers = _tiers()
        assert tiers["pro_plus"]["db_slug"] == "pro_plus"
        assert tiers["pro_plus"]["price_usd"] == 100

    def test_pro_price_unchanged_at_9_95(self):
        """M2 does not own bundle_limit (M1) or touch Pro's price (D-004)."""
        from app.tier_labels import _tiers

        tiers = _tiers()
        assert tiers["pro"]["price_usd"] == 9.95


class TestIsPublicTierHelper:
    def test_free_is_public(self):
        from app.tier_labels import is_public_tier

        assert is_public_tier("free") is True

    def test_pro_is_public(self):
        from app.tier_labels import is_public_tier

        assert is_public_tier("pro") is True

    def test_pro_plus_is_not_public(self):
        from app.tier_labels import is_public_tier

        assert is_public_tier("pro_plus") is False

    def test_legacy_operator_slug_resolves_to_pro_plus_not_public(self):
        from app.tier_labels import is_public_tier

        assert is_public_tier("operator") is False

    def test_unknown_tier_defaults_public(self):
        """Fail open on presentation for an unrecognized slug — never hide
        a real tier by accident because of a typo'd lookup."""
        from app.tier_labels import is_public_tier

        assert is_public_tier("made_up_tier") is True

    def test_none_defaults_public(self):
        from app.tier_labels import is_public_tier

        assert is_public_tier(None) is True


class TestTierLabelsStillResolvesProPlus:
    """DoD: tier_labels.py must keep resolving pro_plus (label + limit)."""

    def test_display_label_pro_plus(self):
        from app.tier_labels import display_label

        assert display_label("pro_plus") == "Pro+"

    def test_bundle_limit_pro_plus(self):
        from app.tier_labels import bundle_limit

        assert bundle_limit("pro_plus") == 200


class TestSubscriptionTierColumnAcceptsProPlus:
    """DoD: `select distinct subscription_tier from users` must still accept
    pro_plus after the public-ladder change. subscription_tier is a plain
    String(32) column (no DB enum/CHECK constraint) — this test proves the
    ORM/DB layer round-trips the value end to end, not just that the column
    type would theoretically allow it.
    """

    def test_pro_plus_user_round_trips_through_db(self, db_session):
        user = User(
            id=uuid.uuid4(),
            email="proplus-persists@example.com",
            display_name="Pro Plus Persists",
            subscription_tier="pro_plus",
            subscription_status="active",
            subscription_id="sub_test_persists",
        )
        db_session.add(user)
        db_session.commit()

        distinct_tiers = {
            row[0] for row in db_session.query(User.subscription_tier).distinct().all()
        }
        assert "pro_plus" in distinct_tiers

        reloaded = db_session.query(User).filter(User.email == "proplus-persists@example.com").one()
        assert reloaded.subscription_tier == "pro_plus"
