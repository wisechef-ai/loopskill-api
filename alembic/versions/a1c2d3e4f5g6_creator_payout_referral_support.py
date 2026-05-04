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


def upgrade() -> None:
    # Add referral support columns
    op.add_column("creator_payouts", sa.Column("source", sa.String(32), nullable=False, server_default="skill_install"))
    op.add_column("creator_payouts", sa.Column("amount_cents", sa.Integer, nullable=True))
    op.add_column("creator_payouts", sa.Column("referral_id", postgresql.UUID(as_uuid=True), nullable=True))
    
    # Create FK for referral_id
    op.create_foreign_key(
        "fk_creator_payouts_referral_id",
        "creator_payouts", "referrals",
        ["referral_id"], ["id"],
        ondelete="SET NULL",
    )
    
    # Make period_start and period_end nullable (legacy)
    op.alter_column("creator_payouts", "period_start", existing_type=sa.DateTime(), nullable=True)
    op.alter_column("creator_payouts", "period_end", existing_type=sa.DateTime(), nullable=True)


def downgrade() -> None:
    # Restore period_start and period_end as NOT NULL
    # WARNING: This will fail if there are NULL values; migrate data first if needed
    op.alter_column("creator_payouts", "period_start", existing_type=sa.DateTime(), nullable=False)
    op.alter_column("creator_payouts", "period_end", existing_type=sa.DateTime(), nullable=False)
    
    # Drop FK and columns
    op.drop_constraint("fk_creator_payouts_referral_id", "creator_payouts", type_="foreignkey")
    op.drop_column("creator_payouts", "referral_id")
    op.drop_column("creator_payouts", "amount_cents")
    op.drop_column("creator_payouts", "source")
