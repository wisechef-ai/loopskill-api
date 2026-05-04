"""add referral fields to users and rate to referrals

Revision ID: a1b2c3d4e5f6
Revises: f1a2c3d4e5b6
Create Date: 2026-05-02 08:00:00.000000

WIS-660: Affiliate tracking + referral revenue share.

Changes:
- users: add referral_code (VARCHAR 16, unique, nullable)
- users: add referred_by (UUID FK → users.id, nullable)
- referrals: add rate (NUMERIC(5,4), default 0.50)
- referrals: drop unique constraint on referral_code (keep index)
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "f1a2c3d4e5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add referral_code and referred_by to users
    op.add_column("users", sa.Column("referral_code", sa.String(16), nullable=True))
    op.add_column("users", sa.Column("referred_by", postgresql.UUID(as_uuid=True), nullable=True))

    # Create indexes
    op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)
    op.create_index("ix_users_referred_by", "users", ["referred_by"], unique=False)

    # Foreign key for referred_by
    op.create_foreign_key(
        "fk_users_referred_by_users",
        "users", "users",
        ["referred_by"], ["id"],
        ondelete="SET NULL",
    )

    # Add rate column to referrals
    op.add_column(
        "referrals",
        sa.Column("rate", sa.Numeric(precision=5, scale=4), nullable=False, server_default="0.50"),
    )

    # Drop unique constraint on referrals.referral_code (keep index for lookups)
    # Guard: constraint may not exist if the table was created without unique=True
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.table_constraints "
        "WHERE constraint_name = 'referrals_referral_code_key' "
        "AND table_name = 'referrals'"
    ))
    if result.fetchone():
        op.drop_constraint("referrals_referral_code_key", "referrals", type_="unique")


def downgrade() -> None:
    # Restore unique constraint on referral_code
    op.create_unique_constraint("referrals_referral_code_key", "referrals", ["referral_code"])

    # Drop rate from referrals
    op.drop_column("referrals", "rate")

    # Drop FK and columns from users
    op.drop_constraint("fk_users_referred_by_users", "users", type_="foreignkey")
    op.drop_index("ix_users_referred_by", table_name="users")
    op.drop_index("ix_users_referral_code", table_name="users")
    op.drop_column("users", "referred_by")
    op.drop_column("users", "referral_code")
