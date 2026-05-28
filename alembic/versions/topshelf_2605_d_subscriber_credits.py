"""topshelf_2605/D — contributor-discount subscriber_credits table

Revision ID: ts2605_d_subscriber_credits
Revises: ts2605_b_feedback_status
Create Date: 2026-05-28 00:00:00.000000

Adds ``subscriber_credits`` table that tracks per-user discount credits.
Pro/pro_plus subscribers who publish skills that get approved receive a
50% discount on their next renewal.

Schema:
  id                        UUID PK
  user_id                   FK → users(id)
  type                      TEXT CHECK ('contributor_discount')
  amount_pct                INT CHECK (1..100)
  granted_for_skill_id      FK → skills(id), nullable
  granted_at                TIMESTAMPTZ DEFAULT NOW()
  expires_at                TIMESTAMPTZ NOT NULL
  used_at                   TIMESTAMPTZ, nullable (NULL = unused)
  used_on_stripe_invoice_id TEXT, nullable

Index: partial index on (user_id) WHERE used_at IS NULL for fast
       "does this user have an unused credit?" lookups.

DOWNGRADE: DROP TABLE subscriber_credits
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision = "ts2605_d_subscriber_credits"
down_revision = "ts2605_b_feedback_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create subscriber_credits table with partial index on unused credits."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Use Postgres-native UUID type on PG, Text elsewhere (SQLite tests).
    uuid_type = PG_UUID(as_uuid=True) if is_postgres else sa.Text()

    op.create_table(
        "subscriber_credits",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("amount_pct", sa.Integer(), nullable=False),
        sa.Column(
            "granted_for_skill_id",
            uuid_type,
            sa.ForeignKey("skills.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_on_stripe_invoice_id", sa.Text(), nullable=True),
        sa.CheckConstraint("type IN ('contributor_discount')", name="ck_sc_type"),
        sa.CheckConstraint(
            "amount_pct > 0 AND amount_pct <= 100", name="ck_sc_amount_pct"
        ),
    )

    # Partial index: fast lookup for unused credits per user.
    # Only Postgres supports partial indexes; skip for SQLite test environments.
    if is_postgres:
        op.execute(
            """
            CREATE INDEX idx_subscriber_credits_user_unused
            ON subscriber_credits(user_id)
            WHERE used_at IS NULL
            """
        )


def downgrade() -> None:
    """Drop subscriber_credits table (index is dropped automatically)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS idx_subscriber_credits_user_unused")
    op.drop_table("subscriber_credits")
