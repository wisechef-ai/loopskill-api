"""Tests for Stripe Connect service (unit tests, no real API calls)."""

import pytest
from unittest.mock import patch, MagicMock

from app.stripe_service import (
    StripeConnectError,
    create_connect_account,
    create_onboarding_link,
    create_dashboard_link,
    create_transfer,
)


class TestCreateConnectAccount:
    @patch("app.stripe_service.settings")
    @patch("app.stripe_service.stripe.Account.create")
    def test_creates_express_account(self, mock_create, mock_settings):
        mock_settings.STRIPE_SECRET_KEY = "sk_test_123"
        mock_create.return_value = MagicMock(id="acct_test_123")
        from app.models import User
        user = User(
            id="test-id",
            email="creator@test.com",
            display_name="Test Creator",
        )

        account_id = create_connect_account(user)
        assert account_id == "acct_test_123"
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["type"] == "express"
        assert call_kwargs["country"] == "PL"
        assert call_kwargs["default_currency"] == "eur"

    @patch("app.stripe_service.stripe.Account.create")
    def test_handles_stripe_error(self, mock_create):
        import stripe
        mock_create.side_effect = stripe.error.StripeError("API error")
        from app.models import User
        user = User(id="test-id", email="test@test.com", display_name="Test")

        with pytest.raises(StripeConnectError):
            create_connect_account(user)


class TestCreateOnboardingLink:
    @patch("app.stripe_service.stripe.AccountLink.create")
    def test_generates_link(self, mock_create):
        mock_create.return_value = MagicMock(url="https://connect.stripe.com/setup/acct_test")
        url = create_onboarding_link("acct_test", "https://return.url", "https://refresh.url")
        assert "stripe.com" in url

    @patch("app.stripe_service.stripe.AccountLink.create")
    def test_handles_error(self, mock_create):
        import stripe
        mock_create.side_effect = stripe.error.StripeError("fail")
        with pytest.raises(StripeConnectError):
            create_onboarding_link("acct_test", "r", "r")


class TestCreateTransfer:
    @patch("app.stripe_service.stripe.Transfer.create")
    def test_creates_transfer(self, mock_create):
        mock_create.return_value = MagicMock(id="tr_test_123")
        result = create_transfer("acct_test", 5000, "eur", "Test payout")
        assert result.id == "tr_test_123"

    @patch("app.stripe_service.stripe.Transfer.create")
    def test_skips_small_amounts(self, mock_create):
        result = create_transfer("acct_test", 50, "eur", "Tiny payout")
        assert result is None
        mock_create.assert_not_called()

    @patch("app.stripe_service.stripe.Transfer.create")
    def test_includes_transfer_group(self, mock_create):
        mock_create.return_value = MagicMock(id="tr_test")
        create_transfer("acct_test", 5000, "eur", "Test", transfer_group="wr-payout-2026-04")
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["transfer_group"] == "wr-payout-2026-04"


class TestCreateDashboardLink:
    @patch("app.stripe_service.stripe.Account.create_login_link")
    def test_generates_dashboard_link(self, mock_create):
        mock_create.return_value = MagicMock(url="https://dashboard.stripe.com/acct_test")
        url = create_dashboard_link("acct_test")
        assert "stripe.com" in url
