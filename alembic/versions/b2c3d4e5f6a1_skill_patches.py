"""skill_patches

Revision ID: b2c3d4e5f6a1
Revises: fb1f00d000aa
Create Date: 2026-05-09 12:00:00.000000

Creates skill_patches table for the skill-patch-as-PR MCP tool (Stream A).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a1"
down_revision = "fb1f00d000aa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    existing = set(sa.inspect(bind).get_table_names())

    if "skill_patches" in existing:
        return

    uuid_pk_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)
    json_type = postgresql.JSONB() if is_pg else sa.JSON()

    op.create_table(
        "skill_patches",
        sa.Column("id", uuid_pk_type, primary_key=True, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("api_key_h", sa.Text(), nullable=True),
        sa.Column("slug", sa.Text(), nullable=True),
        sa.Column("base_version", sa.Text(), nullable=False),
        sa.Column("dedup_hash", sa.Text(), nullable=False),
        sa.Column("file_paths_json", json_type, nullable=False, server_default="[]"),
        sa.Column("anon_hash", sa.Text(), nullable=False, server_default=""),
        sa.Column("gh_pr_number", sa.Integer(), nullable=True),
        sa.Column("gh_pr_url", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_install_id", sa.Text(), nullable=True),
    )

    op.create_index("idx_sp_api_key_h", "skill_patches", ["api_key_h"])
    op.create_index("idx_sp_slug", "skill_patches", ["slug"])
    op.create_index("idx_sp_dedup_hash", "skill_patches", ["dedup_hash"], unique=True)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skill_patches")
