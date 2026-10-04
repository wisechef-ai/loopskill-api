"""fed1004 — federation_index_cache.upstream_etag for change-driven hub sync

Revision ID: fed1004_hub_upstream_etag
Revises: ah0916_msq_client_ip
Create Date: 2026-10-04 18:00:00.000000

Stores the ETag of the last successful Hermes Hub snapshot ingest, so the hourly
sync can send If-None-Match and skip the ~41 MB download when upstream did not
change. Nullable: NULL means "no conditional fetch yet — do a full ingest".

DOWNGRADE: drop the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "fed1004_hub_upstream_etag"
down_revision = "ah0916_msq_client_ip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("federation_index_cache", sa.Column("upstream_etag", sa.String(256), nullable=True))


def downgrade() -> None:
    op.drop_column("federation_index_cache", "upstream_etag")
