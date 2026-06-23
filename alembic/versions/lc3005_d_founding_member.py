"""loopclose_3005/D — founding integrator columns on users

Revision ID: lc3005_d_founding_member
Revises: ts2605_h_missing_skill_q
Create Date: 2026-06-01 00:00:00.000000

Adds the two columns backing the Founding Integrator SKU (a one-time payment
granting lifetime pro_plus, capped at config/tiers.yaml `founding.slot_cap`):

  founding_member        BOOLEAN  NOT NULL DEFAULT false
  founding_slot_number   INTEGER  NULL, UNIQUE

`founding_slot_number` carries a UNIQUE constraint so the database itself
prevents over-selling the capped seat allocation even under concurrent
webhooks — a second grant racing for the same seat number fails with an
IntegrityError, which the service layer turns into a refund-eligible
FoundingSoldOutError.

DOWNGRADE: drop both columns (and the unique constraint/index).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "lc3005_d_founding_member"
down_revision = "ts2605_h_missing_skill_q"
branch_labels = None
depends_on = None


_UQ_NAME = "uq_users_founding_slot_number"
_IX_NAME = "ix_users_founding_slot_number"


def upgrade() -> None:
    """Add founding_member + founding_slot_number (unique) to users."""
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
    # Unique constraint = the authoritative over-sell guard. Indexed for the
    # MAX(slot)+1 / count lookups in founding_service.
    op.create_index(_IX_NAME, "users", ["founding_slot_number"], unique=True)

    # Postgres: drop the redundant server_default so the column isn't pinned to
    # a DB-level default forever (the ORM sets it explicitly). SQLite can't
    # ALTER COLUMN DROP DEFAULT, so leave it — harmless in test envs.
    if is_postgres:
        op.alter_column("users", "founding_member", server_default=None)


def downgrade() -> None:
    """Drop founding columns + the unique index."""
    op.drop_index(_IX_NAME, table_name="users")
    op.drop_column("users", "founding_slot_number")
    op.drop_column("users", "founding_member")
