"""W2 — unit tests for app.revenue_truth, the one revenue-truth helper.

Every money figure in the app is supposed to come from here. If this module is
wrong, every surface is wrong in the same direction, so it is tested directly
rather than only through its callers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from app import revenue_truth as rt


def _sub(
    unit_amount,
    *,
    interval="month",
    interval_count=1,
    qty=1,
    percent_off=None,
    amount_off=None,
    shape="discount",
    extra_items=(),
):
    items = [
        {
            "quantity": qty,
            "price": {
                "unit_amount": unit_amount,
                "recurring": {"interval": interval, "interval_count": interval_count},
            },
        }
    ]
    items.extend(extra_items)
    sub: dict = {"id": "sub_x", "items": {"data": items}}
    if percent_off is not None or amount_off is not None:
        discount = {"id": "di_1", "coupon": {"percent_off": percent_off, "amount_off": amount_off}}
        if shape == "discounts":
            sub["discounts"] = [discount]
        elif shape == "both":
            # Same discount id via both shapes — must be applied ONCE.
            sub["discount"] = discount
            sub["discounts"] = [discount]
        else:
            sub["discount"] = discount
    return sub


# ── list (gross) vs real (net) ──────────────────────────────────────────────


class TestListAndRealMonthly:
    def test_list_is_gross_before_discount(self):
        assert rt.list_monthly_usd(_sub(995, percent_off=100)) == Decimal("9.95")

    def test_real_is_net_after_discount(self):
        assert rt.real_monthly_usd(_sub(995, percent_off=100)) == Decimal("0.00")

    def test_full_price(self):
        assert rt.real_monthly_usd(_sub(995)) == Decimal("9.95")

    def test_half_off_rounds_half_up(self):
        # 995 * 0.5 = 497.5 cents → $4.98, not $4.97
        assert rt.real_monthly_usd(_sub(995, percent_off=50)) == Decimal("4.98")

    def test_amount_off_subtracts(self):
        assert rt.real_monthly_usd(_sub(10000, amount_off=3000)) == Decimal("70.00")

    def test_discount_clamps_at_zero(self):
        assert rt.real_monthly_usd(_sub(995, amount_off=999999)) == Decimal("0.00")

    def test_quantity_multiplies(self):
        assert rt.real_monthly_usd(_sub(995, qty=3)) == Decimal("29.85")

    def test_yearly_normalised(self):
        assert rt.real_monthly_usd(_sub(24000, interval="year")) == Decimal("20.00")

    def test_interval_count_divides(self):
        # $40 charged every 2 months → $20/mo
        assert rt.real_monthly_usd(_sub(4000, interval="month", interval_count=2)) == Decimal("20.00")

    def test_weekly_and_daily(self):
        assert rt.real_monthly_usd(_sub(1000, interval="week")) == Decimal("43.33")
        assert rt.real_monthly_usd(_sub(100, interval="day")) == Decimal("30.42")

    def test_unknown_interval_is_skipped_not_guessed(self):
        """An interval we don't model must under-count, never invent revenue."""
        assert rt.real_monthly_cents(_sub(995, interval="fortnight")) == Decimal(0)

    def test_multiple_line_items_sum(self):
        extra = {"quantity": 2, "price": {"unit_amount": 500, "recurring": {"interval": "month", "interval_count": 1}}}
        assert rt.real_monthly_usd(_sub(995, extra_items=[extra])) == Decimal("19.95")

    def test_metered_item_contributes_nothing(self):
        assert rt.real_monthly_cents(_sub(None)) == Decimal(0)

    def test_empty_and_none(self):
        assert rt.real_monthly_cents({}) == Decimal(0)
        assert rt.real_monthly_cents(None) == Decimal(0)
        assert rt.list_monthly_usd(None) == Decimal("0.00")

    def test_no_floats_leak_into_the_result(self):
        """Money is Decimal end to end — a float here would reintroduce
        0.1 + 0.2 != 0.3 into a revenue figure."""
        for value in (
            rt.real_monthly_cents(_sub(995, percent_off=50)),
            rt.real_monthly_usd(_sub(995)),
            rt.list_monthly_usd(_sub(995)),
            rt.tier_list_monthly_usd("pro"),
        ):
            assert isinstance(value, Decimal), type(value)

    def test_exact_cents_are_not_prematurely_rounded(self):
        """The unrounded accumulator is what lets a caller sum many subs and
        round once. 995/3 months is not a whole number of cents."""
        cents = rt.real_monthly_cents(_sub(995, interval="month", interval_count=3))
        assert cents != cents.to_integral_value(), cents


# ── Coupon shapes ───────────────────────────────────────────────────────────


class TestCouponShapes:
    @pytest.mark.parametrize("shape", ["discount", "discounts"])
    def test_both_stripe_shapes_are_read(self, shape):
        """Stripe exposes the coupon as a singular object AND as an array. A
        handler that read only one would report list price as revenue for the
        other — the exact bug this module exists to prevent."""
        assert rt.real_monthly_usd(_sub(995, percent_off=100, shape=shape)) == Decimal("0.00")

    def test_same_discount_in_both_shapes_applies_once(self):
        """De-duplicated by discount id: applying a 50% coupon twice would
        under-report revenue by half."""
        assert rt.real_monthly_usd(_sub(1000, percent_off=50, shape="both")) == Decimal("5.00")

    def test_two_distinct_coupons_compound(self):
        sub = _sub(1000)
        sub["discounts"] = [
            {"id": "di_a", "coupon": {"percent_off": 50}},
            {"id": "di_b", "coupon": {"percent_off": 50}},
        ]
        assert rt.real_monthly_usd(sub) == Decimal("2.50")

    def test_malformed_discount_is_ignored(self):
        sub = _sub(995)
        sub["discount"] = "not-a-dict"
        assert rt.real_monthly_usd(sub) == Decimal("9.95")

    def test_discount_without_coupon_is_ignored(self):
        sub = _sub(995)
        sub["discount"] = {"id": "di_x"}
        assert rt.real_monthly_usd(sub) == Decimal("9.95")


# ── The predicate ───────────────────────────────────────────────────────────


class TestIsComped:
    def test_hundred_percent_off_is_comped(self):
        assert rt.is_comped(_sub(995, percent_off=100)) is True

    def test_zero_list_price_is_also_comped(self):
        """The internal '$0 Co-worker price' moves no money either."""
        assert rt.is_comped(_sub(0)) is True

    def test_paid_is_not_comped(self):
        assert rt.is_comped(_sub(995)) is False

    def test_partial_discount_is_not_comped(self):
        assert rt.is_comped(_sub(995, percent_off=99)) is False

    def test_sub_cent_residue_is_not_comped(self):
        """99.99% off $9.95 still bills something; only $0 is comped."""
        assert rt.is_comped(_sub(995, percent_off=Decimal("99.9"))) is False


class TestHasResolvableAmount:
    def test_priced_item_is_resolvable(self):
        assert rt.has_resolvable_amount(_sub(995)) is True

    def test_zero_price_is_resolvable(self):
        """$0.00 is a FACT Stripe reported; it is not 'unknown'."""
        assert rt.has_resolvable_amount(_sub(0)) is True

    def test_metered_item_is_not_resolvable(self):
        assert rt.has_resolvable_amount(_sub(None)) is False

    def test_empty_and_none_are_not_resolvable(self):
        assert rt.has_resolvable_amount({}) is False
        assert rt.has_resolvable_amount(None) is False


class TestDiscountPct:
    def test_full_comp(self):
        assert rt.discount_pct(_sub(995, percent_off=100)) == Decimal(100)

    def test_exact_half_off_is_not_a_rounding_artefact(self):
        """Computed from exact cents. Derived from rounded dollars this reads
        49.95, which looks like a bug in a Discord alert."""
        assert rt.discount_pct(_sub(995, percent_off=50)) == Decimal(50)

    def test_no_discount_is_zero(self):
        assert rt.discount_pct(_sub(995)) == Decimal(0)

    def test_zero_list_price_has_no_percentage(self):
        assert rt.discount_pct(_sub(0)) is None


class TestFiguresFor:
    def test_bundles_three_consistent_numbers(self):
        f = rt.figures_for(_sub(995, percent_off=50))
        assert (f.real_usd, f.list_usd, f.discount_pct) == (
            Decimal("4.98"),
            Decimal("9.95"),
            Decimal(50),
        )

    def test_unknown_is_all_none(self):
        for sub in (None, {}, _sub(None)):
            assert rt.figures_for(sub) == (None, None, None)

    def test_comped_reports_zero_not_none(self):
        """The distinction that keeps a missing object from becoming a hard $0."""
        f = rt.figures_for(_sub(995, percent_off=100))
        assert f.real_usd == Decimal("0.00")
        assert f.real_usd is not None


# ── Tier list prices come from the SSOT ─────────────────────────────────────


class TestTierListPrices:
    def test_matches_config_tiers_yaml(self):
        from pathlib import Path

        tiers = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / "config" / "tiers.yaml").read_text()
        )["tiers"]
        for slug, meta in tiers.items():
            if "price_usd" in meta:
                assert rt.tier_list_monthly_usd(slug) == Decimal(str(meta["price_usd"])).quantize(
                    Decimal("0.01")
                ), slug

    def test_pro_is_the_locked_price(self):
        """Lock #24: Free / Pro $9.95 / Enterprise-on-demand."""
        assert rt.tier_list_monthly_usd("pro") == Decimal("9.95")
        assert rt.tier_list_monthly_usd("free") == Decimal("0.00")

    def test_legacy_slugs_resolve(self):
        assert rt.tier_list_monthly_usd("cook") == rt.tier_list_monthly_usd("pro")  # legacy alias
        assert rt.tier_list_monthly_usd("studio") == rt.tier_list_monthly_usd("pro_plus")  # legacy alias

    def test_unknown_and_none_are_zero(self):
        assert rt.tier_list_monthly_usd("nonexistent") == Decimal("0.00")
        assert rt.tier_list_monthly_usd(None) == Decimal("0.00")

    def test_case_insensitive(self):
        assert rt.tier_list_monthly_usd("PRO") == Decimal("9.95")


class TestHealthyStatuses:
    def test_the_shared_set(self):
        assert rt.HEALTHY_SUB_STATUSES == frozenset({"active", "trialing"})

    def test_every_local_copy_now_points_here(self):
        """Five modules had their own copy; three then forgot to consult it."""
        from app.bundle_routes import ACTIVE_SUB_STATUSES

        assert ACTIVE_SUB_STATUSES == rt.HEALTHY_SUB_STATUSES
