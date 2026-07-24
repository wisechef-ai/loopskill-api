"""RCP-15: Stripe Checkout pre-applies a promotion code passed in body.

Bug history
-----------
Stripe Checkout's "Add promotion code" link is collapsed by default in
the EU layout. Adam reported customers couldn't find where to enter a
discount code on the checkout page. We can't change Stripe's UI, but
we *can* pre-apply a known code so the buyer lands on a checkout that
already shows the discount applied — no extra clicks needed.

Contract pinned:
1. POST /api/checkout/{tier} with {"promo_code": "WELCOME50"} ⇒
   stripe.checkout.Session.create receives discounts=[{promotion_code:
   <id>}] (NOT allow_promotion_codes=True — Stripe disallows both
   together).
2. Empty / missing / falsy promo_code ⇒ allow_promotion_codes=True
   (legacy behaviour, buyer can still enter codes manually).
3. Unknown / inactive promo_code ⇒ silently ignored, falls back to
   allow_promotion_codes=True. The buyer is NEVER blocked at checkout
   over a typo.
4. Stripe API failure during code lookup ⇒ logged, ignored, falls back
   to the legacy behaviour. Buyer is never blocked.
5. Code is normalised to uppercase before lookup so "welcome50",
   "Welcome50", and "WELCOME50" all resolve to the same code (Stripe
   stores codes case-sensitive, so we standardise on upper).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models import User
from app.subscription_service import create_checkout_session


def _make_user() -> User:
    return User(
        id=uuid4(),
        email="buyer@example.com",
        display_name="Test Buyer",
        stripe_customer_id="cus_TEST_BUYER",
    )


def _stub_stripe_session_response() -> dict:
    return {"id": "cs_test_xyz", "url": "https://checkout.stripe.com/c/cs_test_xyz"}


class TestPromoCodePreApplied:
    """The happy path — a valid promo code lands on Stripe pre-applied."""

    def test_valid_code_passes_discounts_kwarg(self):
        with patch("app.subscription_service.stripe") as stripe_mock, \
             patch("app.subscription_service.get_or_create_customer", return_value="cus_TEST_BUYER"), \
             patch("app.subscription_service.TIER_PRICE_IDS", {"pro": "price_TEST_COOK"}):
            stripe_mock.PromotionCode.list.return_value = {
                "data": [{"id": "promo_LIVE_WELCOME50", "code": "WELCOME50"}],
            }
            stripe_mock.checkout.Session.create.return_value = _stub_stripe_session_response()

            user = _make_user()
            db = MagicMock()
            create_checkout_session(
                user=user, tier="pro", db=db, promo_code="WELCOME50",
            )

            stripe_mock.PromotionCode.list.assert_called_once_with(
                code="WELCOME50", active=True, limit=1,
            )
            kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
            assert kwargs["discounts"] == [{"promotion_code": "promo_LIVE_WELCOME50"}], (
                f"Expected discounts kwarg with the resolved promotion_code id, "
                f"got: {kwargs.get('discounts')!r}"
            )
            # Stripe forbids passing both — so allow_promotion_codes must NOT
            # be present when discounts is set.
            assert "allow_promotion_codes" not in kwargs, (
                "Stripe rejects allow_promotion_codes + discounts together. "
                "When a code is pre-applied, allow_promotion_codes must be omitted."
            )

    def test_lowercase_code_is_normalised_to_uppercase(self):
        with patch("app.subscription_service.stripe") as stripe_mock, \
             patch("app.subscription_service.get_or_create_customer", return_value="cus_TEST_BUYER"), \
             patch("app.subscription_service.TIER_PRICE_IDS", {"pro": "price_TEST"}):
            stripe_mock.PromotionCode.list.return_value = {
                "data": [{"id": "promo_LIVE", "code": "WELCOME50"}],
            }
            stripe_mock.checkout.Session.create.return_value = _stub_stripe_session_response()

            create_checkout_session(
                user=_make_user(), tier="pro", db=MagicMock(),
                promo_code="  welcome50  ",  # whitespace + lowercase
            )

            stripe_mock.PromotionCode.list.assert_called_once_with(
                code="WELCOME50", active=True, limit=1,
            )


class TestPromoCodeFallback:
    """Empty / missing / unknown / failing codes never block checkout."""

    def test_no_promo_code_uses_legacy_allow_promotion_codes(self):
        with patch("app.subscription_service.stripe") as stripe_mock, \
             patch("app.subscription_service.get_or_create_customer", return_value="cus_TEST"), \
             patch("app.subscription_service.TIER_PRICE_IDS", {"pro": "price_TEST"}):
            stripe_mock.checkout.Session.create.return_value = _stub_stripe_session_response()

            create_checkout_session(
                user=_make_user(), tier="pro", db=MagicMock(),
                # promo_code intentionally omitted
            )
            kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
            assert kwargs.get("allow_promotion_codes") is True
            assert "discounts" not in kwargs
            stripe_mock.PromotionCode.list.assert_not_called()

    def test_empty_string_promo_code_uses_legacy_behaviour(self):
        with patch("app.subscription_service.stripe") as stripe_mock, \
             patch("app.subscription_service.get_or_create_customer", return_value="cus_TEST"), \
             patch("app.subscription_service.TIER_PRICE_IDS", {"pro": "price_TEST"}):
            stripe_mock.checkout.Session.create.return_value = _stub_stripe_session_response()

            create_checkout_session(
                user=_make_user(), tier="pro", db=MagicMock(), promo_code="",
            )
            kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
            assert kwargs.get("allow_promotion_codes") is True
            assert "discounts" not in kwargs

    def test_unknown_promo_code_falls_back_does_not_block(self):
        with patch("app.subscription_service.stripe") as stripe_mock, \
             patch("app.subscription_service.get_or_create_customer", return_value="cus_TEST"), \
             patch("app.subscription_service.TIER_PRICE_IDS", {"pro": "price_TEST"}):
            stripe_mock.PromotionCode.list.return_value = {"data": []}  # not found
            stripe_mock.checkout.Session.create.return_value = _stub_stripe_session_response()

            result = create_checkout_session(
                user=_make_user(), tier="pro", db=MagicMock(),
                promo_code="DOESNOTEXIST",
            )
            kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
            assert kwargs.get("allow_promotion_codes") is True
            assert "discounts" not in kwargs
            assert result["url"].startswith("https://checkout.stripe.com/")

    def test_stripe_listobject_with_data_attribute_works(self):
        """Stripe SDK 15.x returns ``stripe.ListObject`` with ``.data``
        attribute, not ``.get('data')``. The code must handle both shapes
        — the dict-shape is what test_valid_code_passes_discounts_kwarg
        already exercises; this pins the attribute-shape used by the
        real SDK at runtime.
        """
        with patch("app.subscription_service.stripe") as stripe_mock, \
             patch("app.subscription_service.get_or_create_customer", return_value="cus_TEST"), \
             patch("app.subscription_service.TIER_PRICE_IDS", {"pro": "price_TEST"}):
            # Mock a ListObject-like object: has .data attribute, NO .get method.
            class _ListObject:
                """Minimal stand-in for stripe.ListObject — only .data."""
                def __init__(self, data):
                    self.data = data

            stripe_mock.PromotionCode.list.return_value = _ListObject(
                [{"id": "promo_LIVE_FROM_SDK", "code": "WELCOME50"}]
            )
            stripe_mock.checkout.Session.create.return_value = _stub_stripe_session_response()

            create_checkout_session(
                user=_make_user(), tier="pro", db=MagicMock(),
                promo_code="WELCOME50",
            )
            kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
            assert kwargs.get("discounts") == [{"promotion_code": "promo_LIVE_FROM_SDK"}]
            assert "allow_promotion_codes" not in kwargs

    def test_stripe_listobject_promotion_code_with_id_attr_works(self):
        """The PromotionCode resource itself has a ``.id`` attribute as
        well as supporting dict-style access. Ensure we extract id correctly
        even when the test fixture is an attribute-only object.
        """
        with patch("app.subscription_service.stripe") as stripe_mock, \
             patch("app.subscription_service.get_or_create_customer", return_value="cus_TEST"), \
             patch("app.subscription_service.TIER_PRICE_IDS", {"pro": "price_TEST"}):
            class _PromoCode:
                """SDK-shape promotion code: has .id but no [] support."""
                id = "promo_FROM_OBJ_ATTR"

            class _ListObject:
                data = [_PromoCode()]

            stripe_mock.PromotionCode.list.return_value = _ListObject()
            stripe_mock.checkout.Session.create.return_value = _stub_stripe_session_response()

            create_checkout_session(
                user=_make_user(), tier="pro", db=MagicMock(),
                promo_code="WELCOME50",
            )
            kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
            assert kwargs.get("discounts") == [{"promotion_code": "promo_FROM_OBJ_ATTR"}]

    def test_stripe_api_error_during_lookup_falls_back_does_not_raise(self):
        with patch("app.subscription_service.stripe") as stripe_mock, \
             patch("app.subscription_service.get_or_create_customer", return_value="cus_TEST"), \
             patch("app.subscription_service.TIER_PRICE_IDS", {"pro": "price_TEST"}):
            stripe_mock.PromotionCode.list.side_effect = Exception("Stripe API down")
            stripe_mock.checkout.Session.create.return_value = _stub_stripe_session_response()

            result = create_checkout_session(
                user=_make_user(), tier="pro", db=MagicMock(),
                promo_code="WELCOME50",
            )
            kwargs = stripe_mock.checkout.Session.create.call_args.kwargs
            # Lookup failed → fall through to legacy behaviour. Buyer never blocked.
            assert kwargs.get("allow_promotion_codes") is True
            assert "discounts" not in kwargs
            assert result["url"].startswith("https://checkout.stripe.com/")
