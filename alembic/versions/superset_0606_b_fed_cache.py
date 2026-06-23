"""superset_0606/B — persistent federation_index_cache table

Revision ID: superset_0606_b_fed_cache
Revises: evergreen_g_free_sync
Create Date: 2026-06-06 11:10:00.000000

Adds ``federation_index_cache`` — the persistent per-source index cache that
makes catalog DEPTH viable on the weak origin box. A cold ``/api/skills/external``
load reads cached counts + first-page from here; the expensive cursor/sitemap
walks run in a background reindex cron and write rows here. Survives restart
(the difference from the per-process in-memory _TTLCache).

Schema (one row per source):
  source            TEXT PK         e.g. 'clawhub', 'skills-sh', 'github-anthropic'
  indexed_count     INT NULL        everything discovered; NULL = never walked OK
  installable_count INT NULL        the resolved redistributable subset (decision #5)
  first_page        JSON NULL       cached first page of results (list[dict])
  walked_at         TIMESTAMPTZ NULL  when the last successful walk completed
  ttl_seconds       INT NOT NULL DEFAULT 86400  staleness window (daily for giants)
  last_error        TEXT NULL       last walk failure message, if any
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()  row touch time

DOWNGRADE: DROP TABLE federation_index_cache
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "superset_0606_b_fed_cache"
down_revision = "evergreen_g_free_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the federation_index_cache table."""
    op.create_table(
        "federation_index_cache",
        sa.Column("source", sa.String(length=64), primary_key=True),
        sa.Column("indexed_count", sa.Integer(), nullable=True),
        sa.Column("installable_count", sa.Integer(), nullable=True),
        sa.Column("first_page", sa.JSON(), nullable=True),
        sa.Column("walked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )


def downgrade() -> None:
    """Drop the federation_index_cache table."""
    op.drop_table("federation_index_cache")
