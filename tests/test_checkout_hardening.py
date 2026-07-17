"""tests/test_checkout_hardening.py — fix/checkout-hardening (2026-07-17).

Regression suite for three live-found checkout defects:

1. CURRENCY CONFLICT → 409, not opaque 500. Stripe hard-forbids mixing
   currencies on one customer while any subscription is active. Adam's
   customer carried a leftover €0.00 e2e test sub ("Recipes — Operator E2E
   Test") from the recipes drills, which made every USD Pro checkout die
   with InvalidRequestError("You cannot combine currencies…") surfaced as a
   bare 500 {"detail": "checkout_error"}.

2. BROWSER GET on /api/checkout/{tier} → 303 to /pricing, not 405. The
   pricing page's sign-in CTA links to /signin?next=/api/checkout/pro; after
   OAuth the browser GET-lands on the POST-only route and hit a dead-end
   405 (Adam reproduced it live).

3. Webhook boot-check constant points at app.loopskill.io (rename artifact:
   it still said recipes.wisechef.ai, so every boot logged a false
   "zero enabled endpoints" CRITICAL and posted a #tori alert).

Mirrors the mock pattern of tests/test_stripe_checkout.py.
"""

from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import User


def _make_user(db: Session, email: str = "hardening@example.com", **kwargs) -> User:
    """Create and flush a User row for testing."""
    defaults = dict(
        display_name="Hardening TestUser",
        github_id=88888,
        subscription_status=None,
        subscription_tier=None,
    )
    defaults.update(kwargs)
    user = User(id=uuid4(), email=email, **defaults)
    db.add(user)
    db.flush()
    return user


class TestCurrencyConflict:
    """Defect 1 — Stripe currency-mix errors become an actionable 409."""

    @patch("app.subscription_service.stripe")
    def test_currency_conflict_returns_409(self, mock_stripe, client, db_session):
        """The exact live failure: combine-currencies InvalidRequestError → 409."""
        import stripe as stripe_real

        from app.checkout_routes import get_current_user_optional

        user = _make_user(db_session, stripe_customer_id="cus_eur_locked")
        mock_stripe.checkout.Session.create.side_effect = stripe_real.InvalidRequestError(
            "You cannot combine currencies on a single customer. This customer has an "
            "active subscription, subscription schedule, discount, quote, invoice item "
            "or active subscription mode checkout session with currency eur.",
            param=None,
        )

        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        with patch(
            "app.subscription_service.TIER_PRICE_IDS",
            {"pro": "price_test_pro", "pro_plus": "price_test_pro_plus"},
        ):
            try:
                resp = client.post("/api/checkout/pro")
            finally:
                client.app.dependency_overrides.pop(get_current_user_optional, None)

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["reason"] == "currency_conflict"
        assert "currency" in detail["message"].lower()

    @patch("app.subscription_service.stripe")
    def test_other_invalid_request_errors_stay_500(self, mock_stripe, client, db_session):
        """Non-currency InvalidRequestErrors keep the generic 500 contract."""
        import stripe as stripe_real

        from app.checkout_routes import get_current_user_optional

        user = _make_user(db_session, email="other-err@example.com", stripe_customer_id="cus_x")
        mock_stripe.checkout.Session.create.side_effect = stripe_real.InvalidRequestError(
            "No such price: price_gone", param="price"
        )

        client.app.dependency_overrides[get_current_user_optional] = lambda: user
        with patch(
            "app.subscription_service.TIER_PRICE_IDS",
            {"pro": "price_test_pro", "pro_plus": "price_test_pro_plus"},
        ):
            try:
                resp = client.post("/api/checkout/pro")
            finally:
                client.app.dependency_overrides.pop(get_current_user_optional, None)

        assert resp.status_code == 500, resp.text
        assert resp.json()["detail"] == "checkout_error"


class TestCheckoutGetRedirect:
    """Defect 2 — browser GETs redirect to /pricing instead of 405."""

    def test_get_checkout_redirects_to_pricing(self, client):
        """GET /api/checkout/pro → 303 /pricing (the OAuth-next landing case)."""
        resp = client.get("/api/checkout/pro", follow_redirects=False)
        assert resp.status_code == 303, resp.text
        assert resp.headers["location"] == "/pricing"

    def test_get_checkout_any_tier_redirects(self, client):
        """Legacy and garbage tier segments also land on /pricing (no 405s)."""
        for tier in ("pro_plus", "cook", "operator", "nonsense"):
            resp = client.get(f"/api/checkout/{tier}", follow_redirects=False)
            assert resp.status_code == 303, f"{tier}: {resp.status_code}"
            assert resp.headers["location"] == "/pricing"

    def test_post_contract_unchanged_anonymous_401(self, client):
        """POST keeps its existing contract: anonymous → 401 login_required."""
        resp = client.post("/api/checkout/pro")
        assert resp.status_code == 401, resp.text
        assert resp.json()["detail"] == "login_required"


class TestWebhookUrlConstant:
    """Defect 3 — boot-check expects the loopskill URL, not the recipes one."""

    def test_expected_webhook_url_is_loopskill(self):
        from app.startup_checks import EXPECTED_WEBHOOK_URL

        assert EXPECTED_WEBHOOK_URL == "https://app.loopskill.io/api/stripe/webhook"
