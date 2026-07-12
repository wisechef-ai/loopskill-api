"""add read-only public bundle follows

Revision ID: liked0711_p2
Revises: liked0711_p0
Create Date: 2026-07-12
"""

from alembic import op
import sqlalchemy as sa


revision = "liked0711_p2"
down_revision = "liked0711_p0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the follower-to-public-bundle reference table."""
    op.create_table(
        "followed_bundles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("bundle_id", sa.Uuid(), nullable=False),
        sa.Column(
            "followed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bundle_id"], ["bundles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "bundle_id", name="uq_followed_bundles_user_bundle"),
    )
    op.create_index("ix_followed_bundles_user_id", "followed_bundles", ["user_id"])
    op.create_index("ix_followed_bundles_bundle_id", "followed_bundles", ["bundle_id"])


def downgrade() -> None:
    """Remove saved bundle follows."""
    op.drop_index("ix_followed_bundles_bundle_id", table_name="followed_bundles")
    op.drop_index("ix_followed_bundles_user_id", table_name="followed_bundles")
    op.drop_table("followed_bundles")
