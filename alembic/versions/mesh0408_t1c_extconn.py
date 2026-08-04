"""mesh0408_t1c_external_connectors — staging table for Connector federation

Revision ID: mesh0408_t1c_extconn
Revises: ah0803_repo_pack_tags
Create Date: 2026-08-04

mesh_0408 Phase T1-C. Creates ``external_connectors`` — the staging table that
holds MCP-server candidates pulled from open catalogs (docker/mcp-registry,
modelcontextprotocol/servers, the official MCP registry) BEFORE any human
review. ``review_required`` defaults TRUE at the database level (not just the
ORM default) so even a raw INSERT that omits the column lands in the
unreviewed state — the review gate is enforced at the schema, not merely by
application code discipline.

Postgres-only note (mesh0408 T0-A CI now runs both engines): ``server_default
"true"`` is a valid boolean literal on both Postgres and SQLite (SQLAlchemy's
Boolean type maps it dialect-appropriately), so no dialect branch is needed
here — this migration is a plain ``create_table`` and needs no ALTER-path
SQLite/Postgres divergence.

DOWNGRADE: DROP TABLE external_connectors.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "mesh0408_t1c_extconn"
down_revision: Union[str, Sequence[str], None] = "ah0803_repo_pack_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    uuid_type = PG_UUID(as_uuid=True) if is_postgres else sa.String(36)
    uuid_default = sa.text("gen_random_uuid()") if is_postgres else None

    op.create_table(
        "external_connectors",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("connector_type", sa.String(length=32), nullable=True),
        sa.Column("config_template", sa.JSON(), nullable=True),
        sa.Column("origin_url", sa.Text(), nullable=True),
        sa.Column("license", sa.String(length=128), nullable=True),
        sa.Column("trust_tier", sa.String(length=32), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()") if is_postgres else sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()") if is_postgres else sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("source", "external_id", name="uq_external_connector_source_id"),
    )
    op.create_index("ix_external_connectors_source", "external_connectors", ["source"])
    op.create_index("ix_external_connectors_slug", "external_connectors", ["slug"])


def downgrade() -> None:
    op.drop_index("ix_external_connectors_slug", table_name="external_connectors")
    op.drop_index("ix_external_connectors_source", table_name="external_connectors")
    op.drop_table("external_connectors")
