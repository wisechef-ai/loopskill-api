"""autopilot_0308 M2 — marketing_snapshot() presents exactly the public ladder.

D-003: Free / Pro / Enterprise-on-demand. pro_plus is a live db_slug (see
tests/test_tier_public_ladder.py) but must not appear in the public-facing
tiers block this endpoint serves to /pricing and other marketing surfaces.
Enterprise is a contact form, not a price: no Stripe price object, no new
db_slug — it never appears in config/tiers.yaml or TIER_PRICE_IDS.
"""

from __future__ import annotations

from app.marketing_routes import marketing_snapshot
from app.subscription_service import TIER_PRICE_IDS


def test_pro_plus_not_in_snapshot_tiers(db_session) -> None:
    snap = marketing_snapshot(db_session)
    assert "pro_plus" not in snap["tiers"]


def test_enterprise_present_as_contact_only(db_session) -> None:
    snap = marketing_snapshot(db_session)
    assert "enterprise" in snap["tiers"]
    enterprise = snap["tiers"]["enterprise"]
    assert enterprise.get("price_usd") is None
    assert enterprise.get("contact_only") is True


def test_enterprise_has_no_stripe_price_object(db_session) -> None:
    """Enterprise is a sales conversation, not an automated meter (hub D-005).
    It must never resolve to a real Stripe price id."""
    assert "enterprise" not in TIER_PRICE_IDS


def test_pro_still_present_with_unchanged_price(db_session) -> None:
    snap = marketing_snapshot(db_session)
    assert "pro" in snap["tiers"]
    assert float(snap["tiers"]["pro"]["price_usd"]) == 9.95


def test_snapshot_tiers_are_exactly_the_public_ladder(db_session) -> None:
    """Public ladder is exactly 3: free (implicit — no bullets card today,
    unchanged from pre-M2 behavior), pro, enterprise. pro_plus must never
    resurface here regardless of what future tiers get added."""
    snap = marketing_snapshot(db_session)
    assert set(snap["tiers"].keys()) == {"pro", "enterprise"}
