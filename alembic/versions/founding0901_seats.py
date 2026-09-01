"""feat/founding — users.founding_member + users.founding_slot_number

Revision ID: founding0901_seats
Revises: issue282_fed_trgm
Create Date: 2026-09-01

Adds the two columns that back the $49 one-time Founding Member SKU (capped
100 seats — see config/tiers.yaml's `founding:` sibling key and
app/services/founding_service.py):

  users.founding_member       BOOLEAN NOT NULL DEFAULT false
  users.founding_slot_number  INTEGER UNIQUE, nullable

``founding_slot_number`` is the DB-authoritative over-sell guard: a UNIQUE
index means two concurrent grants racing for the same MAX(slot)+1 value
cannot both commit, closing the over-sell window Postgres itself enforces
(stripe-one-time-sku-on-subscription-rail Trap 3). Additive-only, no data
migration — existing users get founding_member=false, founding_slot_number
NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "founding0901_seats"
down_revision = "issue282_fed_trgm"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("founding_member", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("founding_slot_number", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_users_founding_slot_number",
        "users",
        ["founding_slot_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_users_founding_slot_number", table_name="users")
    op.drop_column("users", "founding_slot_number")
    op.drop_column("users", "founding_member")
