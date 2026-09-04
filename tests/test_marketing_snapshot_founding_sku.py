"""founding0904 — the $49 one-time Founding SKU is VISIBLE on the marketing snapshot.

Defect (2026-09-04, atomic-habits rank 1): PR #304 (loopskill-api) and #101
(loopskill-portal) shipped the capped-100 $49 one-time Founding Member SKU and
the live seat counter at GET /api/founding/remaining, but the LIVE
/api/marketing/snapshot `tiers` object contained only `pro` and `enterprise`.
Every public surface that reads the snapshot for pricing therefore advertised a
product line that omitted the one SKU built to convert.

The fix overlays a TOP-LEVEL `founding` key — a SIBLING of `tiers`, never a
member of it. That split is load-bearing and is the same one config/tiers.yaml
already uses (see its `founding:` block + app/services/founding_service.py's
module docstring): every loader that iterates `tiers` — the price overlay in
marketing_snapshot, the tier picker, recipes_stripe_sync.py — must stay
structurally unable to see a one-time SKU, or it can be created as a recurring
subscription. `test_founding_is_not_inside_tiers` is that guard, and
tests/test_marketing_snapshot_public_ladder.py's exact-ladder assertion
(`{"pro", "enterprise"}`) is its independent second witness.

Also pinned here: the numbers are LIVE (founding_service + the DB seat counter),
never a second copy in config/recipes-marketing.yaml — the same Class-B
discipline drift_0604 applied to prices and mcp_tools.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import yaml

from app.config import settings
from app.marketing_routes import marketing_snapshot
from app.models import User

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _founding_price_configured(monkeypatch):
    """Give the SKU a Stripe price id for the duration of this module.

    The test environment ships `WR_STRIPE_PRICE_FOUNDING` empty, and the
    snapshot overlay deliberately FAILS CLOSED on an unset price id (a
    configured-but-unpurchasable seat must not be advertised — see
    TestFailsClosed::test_omitted_when_no_stripe_price_id, which asserts
    exactly that and overrides this fixture locally).

    Patching the SETTING rather than founding_price_id() keeps the real
    env-var resolution path under test instead of stubbing it out.
    """
    monkeypatch.setattr(settings, "STRIPE_PRICE_FOUNDING", "price_test_founding", raising=False)


def _make_user(db, **kwargs) -> User:
    user = User(
        id=uuid4(),
        email=f"founding-snap-{uuid4()}@example.com",
        display_name="Snapshot TestUser",
        github_id=None,
        **kwargs,
    )
    db.add(user)
    db.flush()
    return user


# ── 1: the SKU is actually visible ───────────────────────────────────────


class TestFoundingVisibleOnSnapshot:
    def test_founding_block_present(self, db_session) -> None:
        """The regression this file exists for: the block was entirely absent."""
        snap = marketing_snapshot(db_session)
        assert "founding" in snap, "founding SKU missing from the marketing snapshot"

    def test_founding_carries_the_live_price_and_cap(self, db_session) -> None:
        from app.services.founding_service import founding_price_usd, founding_slot_cap

        snap = marketing_snapshot(db_session)
        f = snap["founding"]
        assert float(f["price_usd"]) == float(founding_price_usd())
        assert int(f["cap"]) == int(founding_slot_cap())

    def test_founding_is_marked_one_time_not_a_subscription(self, db_session) -> None:
        """A consumer must be able to tell this apart from a monthly tier
        WITHOUT parsing prose — it is a mode=payment SKU."""
        snap = marketing_snapshot(db_session)
        assert snap["founding"]["one_time"] is True

    def test_founding_exposes_its_checkout_path(self, db_session) -> None:
        snap = marketing_snapshot(db_session)
        assert snap["founding"]["checkout_path"] == "/api/checkout/founding"

    def test_price_renders_as_an_integer_when_whole(self, db_session) -> None:
        """$49, never $49.0 — mirrors overlay (3)'s int-when-whole rule so
        marketing copy never renders a trailing .0."""
        snap = marketing_snapshot(db_session)
        assert snap["founding"]["price_usd"] == 49


# ── 2: the structural guard — sibling of tiers, NEVER a member ───────────


class TestFoundingIsNotATier:
    def test_founding_is_not_inside_tiers(self, db_session) -> None:
        """THE load-bearing assertion. A one-time SKU inside `tiers` would be
        seen by every subscription loader, including the Stripe sync."""
        snap = marketing_snapshot(db_session)
        assert "founding" not in snap["tiers"]

    def test_public_ladder_is_unchanged(self, db_session) -> None:
        """Independent second witness — the pre-existing exact-ladder contract
        from test_marketing_snapshot_public_ladder.py must still hold."""
        snap = marketing_snapshot(db_session)
        assert set(snap["tiers"].keys()) == {"pro", "enterprise"}

    def test_founding_absent_from_the_tier_price_overlay(self, db_session) -> None:
        """The price overlay iterates snap['tiers'] against _load_tier_usd_price().
        Founding must be invisible to it — it has no recurring price."""
        from app.subscription_service import _load_tier_usd_price

        assert "founding" not in _load_tier_usd_price()


# ── 3: numbers are LIVE, not a second static copy ────────────────────────


class TestNoStaticNumberDrift:
    def test_marketing_yaml_founding_block_carries_no_numbers(self) -> None:
        """drift_0604 discipline: config/recipes-marketing.yaml holds Class-C
        PROSE only. A price/cap copied here is a second source that rots."""
        with open(REPO_ROOT / "config" / "recipes-marketing.yaml") as f:
            data = yaml.safe_load(f) or {}
        block = data.get("founding") or {}
        for forbidden in ("price_usd", "cap", "slot_cap", "remaining"):
            assert forbidden not in block, (
                f"config/recipes-marketing.yaml hardcodes founding.{forbidden}; "
                "it must be live-overlaid from founding_service instead"
            )

    def test_remaining_tracks_the_db_seat_counter(self, db_session) -> None:
        """`remaining` is derived from the DB, so granting a seat moves it."""
        before = marketing_snapshot(db_session)["founding"]["remaining"]

        from app.services.founding_service import grant_founding_membership

        grant_founding_membership(_make_user(db_session), db_session)

        after = marketing_snapshot(db_session)["founding"]["remaining"]
        assert after == before - 1, "remaining did not follow the live seat counter"

    def test_bullets_interpolate_live_seat_numbers(self, db_session) -> None:
        """The '{remaining} of {cap}' bullet must be filled, never served raw."""
        snap = marketing_snapshot(db_session)
        bullets = snap["founding"].get("bullets") or []
        joined = " ".join(b for b in bullets if isinstance(b, str))
        assert "{remaining}" not in joined and "{cap}" not in joined, (
            "founding bullets served with uninterpolated placeholders"
        )
        assert str(snap["founding"]["remaining"]) in joined


# ── 4: fail-closed — never advertise an unbuyable seat ───────────────────


class TestFailsClosed:
    def test_omitted_when_sold_out(self, db_session) -> None:
        """A sold-out SKU must vanish from the surface, not render a dead CTA."""
        with patch("app.services.founding_service.founding_seats_remaining", return_value=0):
            snap = marketing_snapshot(db_session)
        assert "founding" not in snap

    def test_omitted_when_not_configured(self, db_session) -> None:
        with patch("app.services.founding_service.founding_configured", return_value=False):
            snap = marketing_snapshot(db_session)
        assert "founding" not in snap

    def test_omitted_when_no_stripe_price_id(self, db_session) -> None:
        """Configured but unpurchasable (no Stripe price wired) is still a
        seat nobody can buy — do not advertise it."""
        with patch("app.services.founding_service.founding_price_id", return_value=""):
            snap = marketing_snapshot(db_session)
        assert "founding" not in snap

    def test_service_failure_degrades_instead_of_500ing(self, db_session) -> None:
        """A founding hiccup must never take down the whole marketing surface."""
        with patch(
            "app.services.founding_service.founding_seats_remaining",
            side_effect=RuntimeError("db down"),
        ):
            snap = marketing_snapshot(db_session)
        assert "founding" not in snap
        assert snap["tiers"]["pro"], "the rest of the snapshot must survive"

    def test_stale_yaml_founding_numbers_cannot_survive(self, db_session) -> None:
        """RED-proof for the pop(): if someone re-adds a stale price to the
        prose yaml, the live overlay must still win (or the key be absent) —
        a stale number must never reach a consumer."""
        with patch("app.services.founding_service.founding_configured", return_value=False):
            snap = marketing_snapshot(db_session)
        assert "founding" not in snap, (
            "a yaml-authored founding block leaked through when the SKU was unconfigured"
        )
