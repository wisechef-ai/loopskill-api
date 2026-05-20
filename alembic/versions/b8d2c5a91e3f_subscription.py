"""subscription columns + stripe events dedup table

Revision ID: b8d2c5a91e3f
Revises: a7f7db696591
Create Date: 2026-04-29 17:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8d2c5a91e3f"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Subscription columns on users
    op.add_column("users", sa.Column("stripe_customer_id", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("subscription_status", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("subscription_tier", sa.String(32), nullable=True))
    op.add_column("users", sa.Column("subscription_id", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("subscription_current_period_end", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_stripe_customer_id", "users", ["stripe_customer_id"], unique=True)
    op.create_index("ix_users_subscription_status", "users", ["subscription_status"])

    # Idempotency table for Stripe webhook events
    op.create_table(
        "stripe_event_ids",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("livemode", sa.Boolean, nullable=True),
    )
    op.create_index("ix_stripe_event_ids_processed_at", "stripe_event_ids", ["processed_at"])


def downgrade() -> None:
    op.drop_index("ix_stripe_event_ids_processed_at", table_name="stripe_event_ids")
    op.drop_table("stripe_event_ids")
    op.drop_index("ix_users_subscription_status", table_name="users")
    op.drop_index("ix_users_stripe_customer_id", table_name="users")
    op.drop_column("users", "subscription_current_period_end")
    op.drop_column("users", "subscription_id")
    op.drop_column("users", "subscription_tier")
    op.drop_column("users", "subscription_status")
    op.drop_column("users", "stripe_customer_id")
