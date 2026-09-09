"""Stripe webhook event-id TTL pruner — t_ae1bddaa.

stripe_event_ids is the webhook idempotency table (WIS-569): one row per
processed Stripe event.id, inserted by
app/subscription_service.record_event_or_skip(). Until now it grew
unboundedly. Stripe's documented redelivery window is ~3 days
(https://docs.stripe.com/webhooks — "Stripe tries again over the course of
three days"); a 7-day TTL comfortably exceeds it, so pruning rows older than
7 days can never re-open a replay window. Idempotent.

Run daily via the systemd timer in deploy/ (see
deploy/recipes-stripe-event-pruner.timer), or manually:
    python -m app.crons.stripe_event_pruner
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import StripeEventId

logger = logging.getLogger(__name__)

# >= 7 days retention is the task spec floor; 7d is ~2.3x Stripe's ~3-day
# redelivery window, so pruning can never re-open a replay.
TTL_DAYS = 7


def prune(
    reference_time: datetime | None = None,
    db: Session | None = None,
) -> int:
    """Delete stripe_event_ids rows older than TTL_DAYS. Returns rows removed.

    Pass `db` to run against an existing session (tests); opens its own
    SessionLocal otherwise.
    """
    cutoff = (reference_time or datetime.now(UTC)) - timedelta(days=TTL_DAYS)
    session = db if db is not None else SessionLocal()
    try:
        deleted = (
            session.query(StripeEventId)
            .filter(StripeEventId.processed_at < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
        logger.info("stripe_event_pruner: removed %d rows older than %s", deleted, cutoff)
        return deleted
    finally:
        if db is None:
            session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    prune()
