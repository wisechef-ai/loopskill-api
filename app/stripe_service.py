"""Stripe Connect Express service for WiseRecipes creator payouts.

Handles:
- Creating Stripe Connect Express accounts for EU creators
- Generating onboarding links
- Creating transfers to connected accounts
- Handling Stripe webhooks (account updates, transfer statuses)
"""

import logging
from typing import Any

import stripe
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User

logger = logging.getLogger(__name__)

# Configure Stripe
stripe.api_key = settings.STRIPE_SECRET_KEY


class StripeConnectError(Exception):
    """Raised when Stripe Connect operations fail."""
    pass


def create_connect_account(user: User) -> str:
    """Create a Stripe Connect Express account for a creator.

    Returns the Stripe account ID.
    """
    if not settings.STRIPE_SECRET_KEY:
        raise StripeConnectError("Stripe not configured (WR_STRIPE_SECRET_KEY missing)")

    try:
        account = stripe.Account.create(
            type="express",
            country="PL",  # Default EU (Poland); creators can change in onboarding
            email=user.email or "",
            metadata={
                "wiserecipes_user_id": str(user.id),
                "platform": "wiserecipes",
            },
            capabilities={
                "transfers": {"requested": True},
            },
            business_type="individual",
            default_currency="eur",
            settings={
                "payouts": {
                    "schedule": {
                        "interval": "monthly",
                        "monthly_anchor": 1,
                    },
                },
            },
        )
        return account.id
    except stripe.error.StripeError as e:
        logger.error(f"Failed to create Connect account for user {user.id}: {e}")
        raise StripeConnectError(f"Stripe account creation failed: {e}")


def create_onboarding_link(account_id: str, return_url: str, refresh_url: str) -> str:
    """Generate a Stripe Connect onboarding link for the creator to complete KYC."""
    try:
        link = stripe.AccountLink.create(
            account=account_id,
            return_url=return_url,
            refresh_url=refresh_url,
            type="account_onboarding",
        )
        return link.url
    except stripe.error.StripeError as e:
        logger.error(f"Failed to create onboarding link for account {account_id}: {e}")
        raise StripeConnectError(f"Onboarding link creation failed: {e}")


def create_dashboard_link(account_id: str) -> str:
    """Generate a Stripe Express dashboard login link for the creator."""
    try:
        link = stripe.Account.create_login_link(account_id)
        return link.url
    except stripe.error.StripeError as e:
        logger.error(f"Failed to create dashboard link for account {account_id}: {e}")
        raise StripeConnectError(f"Dashboard link creation failed: {e}")


def get_account_status(account_id: str) -> dict[str, Any]:
    """Retrieve the Stripe Connect account status."""
    try:
        account = stripe.Account.retrieve(account_id)
        return {
            "id": account.id,
            "charges_enabled": account.charges_enabled,
            "payouts_enabled": account.payouts_enabled,
            "details_submitted": account.details_submitted,
            "country": account.country,
            "default_currency": account.default_currency,
            "capabilities": dict(account.capabilities) if account.capabilities else {},
        }
    except stripe.error.StripeError as e:
        logger.error(f"Failed to retrieve account {account_id}: {e}")
        raise StripeConnectError(f"Account status check failed: {e}")


def create_transfer(
    account_id: str,
    amount_cents: int,
    currency: str,
    description: str,
    metadata: dict[str, str] | None = None,
    transfer_group: str | None = None,
) -> stripe.Transfer | None:
    """Create a transfer to a connected account.

    amount_cents: amount in cents (e.g., 5000 = €50.00)
    """
    if amount_cents < 100:
        # Stripe minimum transfer is typically ~$0.50 / €0.50
        logger.warning(f"Transfer amount {amount_cents} cents is below minimum, skipping")
        return None

    try:
        params: dict[str, Any] = {
            "amount": amount_cents,
            "currency": currency,
            "destination": account_id,
            "description": description,
            "metadata": metadata or {},
        }
        if transfer_group:
            params["transfer_group"] = transfer_group

        transfer = stripe.Transfer.create(**params)
        logger.info(f"Transfer {transfer.id} created: {amount_cents} {currency} -> {account_id}")
        return transfer
    except stripe.error.StripeError as e:
        logger.error(f"Transfer failed for {account_id}: {e}")
        raise StripeConnectError(f"Transfer failed: {e}")


def verify_webhook_signature(payload: bytes, sig_header: str) -> dict:
    """Verify and parse a Stripe webhook event."""
    if not settings.STRIPE_WEBHOOK_SECRET:
        raise StripeConnectError("Stripe webhook secret not configured")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            settings.STRIPE_WEBHOOK_SECRET,
        )
        return event
    except stripe.error.SignatureVerificationError:
        raise StripeConnectError("Invalid webhook signature")
    except Exception as e:
        raise StripeConnectError(f"Webhook parsing failed: {e}")
