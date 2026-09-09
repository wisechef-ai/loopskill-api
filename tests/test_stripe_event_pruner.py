"""Tests for t_ae1bddaa — Stripe webhook event-id idempotency retention.

The idempotency dedupe itself shipped in WIS-569 (stripe_event_ids +
record_event_or_skip + replay no-op, tested in test_subscription.py). This
module pins the missing half of the task spec: the processed-event table is
pruned on a >= 7-day TTL so it can never grow unboundedly, while fresh rows
survive. Also re-verifies the dedupe → pruner seam end-to-end.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.crons.stripe_event_pruner import TTL_DAYS, prune
from app.models import StripeEventId

# ── TTL constant contract ────────────────────────────────────────────────


def test_ttl_is_at_least_seven_days():
    """Task spec floor: retention >= 7 days."""
    assert TTL_DAYS >= 7


# ── Pruner behavior ──────────────────────────────────────────────────────


def test_prune_deletes_only_stale_rows(db_session: Session):
    """Rows past the TTL are deleted; rows within it are untouched."""
    now = datetime.now(UTC)
    stale = now - timedelta(days=TTL_DAYS + 1)
    fresh = now - timedelta(days=1)

    db_session.add(StripeEventId(event_id="evt_stale", event_type="a", processed_at=stale))
    db_session.add(StripeEventId(event_id="evt_fresh", event_type="b", processed_at=fresh))
    db_session.add(
        StripeEventId(event_id="evt_boundary", event_type="c", processed_at=now - timedelta(days=TTL_DAYS))
    )
    db_session.commit()

    deleted = prune(reference_time=now, db=db_session)
    assert deleted == 1

    remaining = {r.event_id for r in db_session.query(StripeEventId).all()}
    assert remaining == {"evt_fresh", "evt_boundary"}


def test_prune_on_empty_table_is_noop(db_session: Session):
    """Pruning an empty table returns 0 and does not raise."""
    assert prune(reference_time=datetime.now(UTC), db=db_session) == 0


def test_prune_is_idempotent(db_session: Session):
    """Second run over the same data deletes nothing more."""
    now = datetime.now(UTC)
    db_session.add(
        StripeEventId(
            event_id="evt_old",
            event_type="a",
            processed_at=now - timedelta(days=TTL_DAYS + 30),
        )
    )
    db_session.commit()

    assert prune(reference_time=now, db=db_session) == 1
    assert prune(reference_time=now, db=db_session) == 0


# ── Dedupe → pruner seam (end-to-end, mirrors WIS-569 Gate 8) ────────────


def test_replay_within_retention_window_is_still_deduped(db_session: Session):
    """A duplicate delivered within the retention window is skipped — the
    invariant the TTL must never break."""
    from app.subscription_service import record_event_or_skip

    event = {"id": "evt_dup_1", "type": "invoice.paid", "livemode": False}
    assert record_event_or_skip(event, db_session) is True  # first sight
    assert record_event_or_skip(event, db_session) is False  # replay skipped

    # Row lands with processed_at; pruner keeps it while fresh.
    row = db_session.get(StripeEventId, "evt_dup_1")
    assert row is not None
    assert prune(reference_time=datetime.now(UTC), db=db_session) == 0
    assert db_session.get(StripeEventId, "evt_dup_1") is not None


@pytest.mark.parametrize("days_old,expect_survives", [(TTL_DAYS - 1, True), (TTL_DAYS + 1, False)])
def test_retention_boundary(db_session: Session, days_old: int, expect_survives: bool):
    """Boundary behavior at exactly the retention edge (strict < cutoff)."""
    now = datetime.now(UTC)
    db_session.add(
        StripeEventId(
            event_id=f"evt_edge_{days_old}",
            event_type="a",
            processed_at=now - timedelta(days=days_old),
        )
    )
    db_session.commit()

    prune(reference_time=now, db=db_session)
    survives = db_session.get(StripeEventId, f"evt_edge_{days_old}") is not None
    assert survives is expect_survives
