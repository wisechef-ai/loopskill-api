"""loopclose_3005/D revert — drop founding integrator columns

Revision ID: lc3005_d_revert_founding
Revises: lc3005_d_founding_member
Create Date: 2026-06-01 14:00:00.000000

Product decision (2026-06-01): the one-time $1000 Founding Integrator SKU is
dropped. Recipes keeps only the recurring $20 Pro / $100 Pro+ rails. This
migration drops the two columns the founding SKU added, keeping alembic history
linear (forward-only) since lc3005_d_founding_member is already applied on PROD.

DOWNGRADE re-adds both columns (mirror of lc3005_d_founding_member upgrade) so
the revert is itself reversible.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "lc3005_d_revert_founding"
down_revision = "lc3005_d_founding_member"
branch_labels = None
depends_on = None


_IX_NAME = "ix_users_founding_slot_number"


def upgrade() -> None:
    """Drop founding_slot_number (+ its unique index) and founding_member."""
    op.drop_index(_IX_NAME, table_name="users")
    op.drop_column("users", "founding_slot_number")
    op.drop_column("users", "founding_member")


def downgrade() -> None:
    """Re-add the founding columns (mirror of lc3005_d_founding_member)."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    op.add_column(
        "users",
        sa.Column(
            "founding_member",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("founding_slot_number", sa.Integer(), nullable=True),
    )
    op.create_index(_IX_NAME, "users", ["founding_slot_number"], unique=True)
    if is_postgres:
        op.alter_column("users", "founding_member", server_default=None)
