"""v7.1 Phase 3 — cookbook_share_tokens table

Revision ID: a3f1e9b5c7d2
Revises: e9b5c7a3f1d8
Create Date: 2026-05-07 16:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a3f1e9b5c7d2"
down_revision = "e9b5c7a3f1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    is_pg = op.get_bind().dialect.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)

    op.create_table(
        "cookbook_share_tokens",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column(
            "cookbook_id",
            uuid_type,
            sa.ForeignKey("cookbooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("token_prefix", sa.String(20), nullable=False),
        sa.Column("scope", sa.String(8), nullable=False, server_default="edit"),
        sa.Column("name", sa.String(120), nullable=True),
        sa.Column(
            "created_by",
            uuid_type,
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope IN ('read', 'edit')", name="ck_cookbook_share_tokens_scope"
        ),
    )
    op.create_index(
        "idx_cbst_prefix", "cookbook_share_tokens", ["token_prefix"],
        if_not_exists=True,
    )
    op.create_index(
        "idx_cbst_cookbook_active", "cookbook_share_tokens",
        ["cookbook_id", "is_active"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("idx_cbst_cookbook_active", table_name="cookbook_share_tokens")
    op.drop_index("idx_cbst_prefix", table_name="cookbook_share_tokens")
    op.drop_table("cookbook_share_tokens")
