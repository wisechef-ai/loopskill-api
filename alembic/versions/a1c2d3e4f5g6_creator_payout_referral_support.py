"""add referral support to creator_payouts

Revision ID: a1c2d3e4f5g6
Revises: a1b2c3d4e5f6
Create Date: 2026-05-02 10:00:00.000000

WIS-660: Referral payout attribution.

Changes:
- creator_payouts: add source (VARCHAR 32, default 'skill_install')
- creator_payouts: add amount_cents (INT, nullable) — for referral payouts
- creator_payouts: add referral_id (UUID FK → referrals.id, nullable)
- creator_payouts: make period_start/period_end nullable (legacy fields)
- creator_payouts: add 'accrued' to status enum
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1c2d3e4f5g6"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def _is_pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    is_pg = _is_pg()
    uuid_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)

    op.add_column("creator_payouts", sa.Column("source", sa.String(32), nullable=False, server_default="skill_install"))
    op.add_column("creator_payouts", sa.Column("amount_cents", sa.Integer, nullable=True))
    op.add_column("creator_payouts", sa.Column("referral_id", uuid_type, nullable=True))

    # FK constraint: Postgres only
    if is_pg:
        op.create_foreign_key(
            "fk_creator_payouts_referral_id",
            "creator_payouts", "referrals",
            ["referral_id"], ["id"],
            ondelete="SET NULL",
        )

    # Make period_start/period_end nullable via batch (required for SQLite).
    with op.batch_alter_table("creator_payouts") as batch_op:
        batch_op.alter_column("period_start", existing_type=sa.DateTime(), nullable=True)
        batch_op.alter_column("period_end", existing_type=sa.DateTime(), nullable=True)


def downgrade() -> None:
    is_pg = _is_pg()

    # Restore period_start/period_end as NOT NULL via batch
    with op.batch_alter_table("creator_payouts") as batch_op:
        batch_op.alter_column("period_start", existing_type=sa.DateTime(), nullable=False)
        batch_op.alter_column("period_end", existing_type=sa.DateTime(), nullable=False)

    if is_pg:
        op.drop_constraint("fk_creator_payouts_referral_id", "creator_payouts", type_="foreignkey")
    with op.batch_alter_table("creator_payouts") as batch_op:
        batch_op.drop_column("referral_id")
        batch_op.drop_column("amount_cents")
        batch_op.drop_column("source")
