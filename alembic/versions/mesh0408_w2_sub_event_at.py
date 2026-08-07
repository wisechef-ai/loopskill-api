"""mesh0408e2e W2 — users.subscription_event_at (Stripe webhook ordering guard)

Revision ID: mesh0408_w2_sub_event_at
Revises: mesh0408_t1c_extconn
Create Date: 2026-08-07

Stripe webhook delivery is at-least-once but **NOT ordered**. An older
``customer.subscription.updated`` carrying ``status=active`` can be delivered
AFTER a newer one carrying ``status=past_due`` and overwrite it — silently
handing Pro entitlement back to a subscription whose card has already failed.

Idempotency does not help: those are two distinct events with distinct ids, so
the ``stripe_event_ids`` primary key accepts both. Signature verification does
not help either. Ordering/staleness is a third, independent property, and it
needed somewhere to store a watermark.

This column holds the Stripe ``event.created`` timestamp of the most recently
APPLIED subscription-state event for the user. ``app.subscription_service``
drops any event older than it. NULL = nothing applied yet, so existing rows
(and every replay of history) behave exactly as before.

Nullable with no server default, so the ALTER is instant on Postgres and needs
no backfill. Timezone-aware to match the sibling ``subscription_*`` columns;
SQLite ignores the tz flag, which is why the ORM re-attaches UTC on read.

DOWNGRADE: drop the column (batch_alter_table so SQLite can rebuild the table).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "mesh0408_w2_sub_event_at"
down_revision: Union[str, Sequence[str], None] = "mesh0408_t1c_extconn"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("subscription_event_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("subscription_event_at")
