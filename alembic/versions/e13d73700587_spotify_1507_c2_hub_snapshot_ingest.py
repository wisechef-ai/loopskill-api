"""spotify_1507_c2_hub_snapshot_ingest

Revision ID: e13d73700587
Revises: e551aae04e88
Create Date: 2026-07-15 19:38:47.542034

Adds the federation_hub_skills table (bulk-ingested Hermes Hub snapshot rows)
and two new columns on federation_index_cache for deduped counting + freshness.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e13d73700587"
down_revision: Union[str, Sequence[str], None] = "e551aae04e88"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # New columns on federation_index_cache for hub snapshot dedup + freshness.
    op.add_column(
        "federation_index_cache",
        sa.Column("deduped_indexed_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "federation_index_cache",
        sa.Column("snapshot_generated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # New table for individual hub-snapshot skill rows.
    op.create_table(
        "federation_hub_skills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="hermes-hub"),
        sa.Column("upstream_source", sa.String(length=64), nullable=True),
        sa.Column("identifier", sa.String(length=512), nullable=True),
        sa.Column("origin_url", sa.Text(), nullable=True),
        sa.Column("install_path", sa.String(length=32), nullable=False, server_default="deep_link"),
        sa.Column("trust_level", sa.String(length=32), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("duplicate_of", sa.String(length=64), nullable=True),
        sa.Column("repo", sa.String(length=512), nullable=True),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_federation_hub_skills_slug"),
    )
    op.create_index(
        "ix_federation_hub_skills_slug",
        "federation_hub_skills",
        ["slug"],
        unique=True,
    )
    op.create_index(
        "ix_federation_hub_skills_upstream_source",
        "federation_hub_skills",
        ["upstream_source"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_federation_hub_skills_upstream_source", table_name="federation_hub_skills")
    op.drop_index("ix_federation_hub_skills_slug", table_name="federation_hub_skills")
    op.drop_table("federation_hub_skills")
    op.drop_column("federation_index_cache", "snapshot_generated_at")
    op.drop_column("federation_index_cache", "deduped_indexed_count")
