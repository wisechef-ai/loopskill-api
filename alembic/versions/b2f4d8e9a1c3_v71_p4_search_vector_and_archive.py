"""v71_p4_search_vector_and_archive

Adds the columns Phase 4 (BM25 reindex) introduced in the Skill model but
forgot to migrate. Idempotent (uses ADD COLUMN IF NOT EXISTS) so it's safe
to apply on prod where we already hot-fixed via raw psql. Pure additive,
fully reversible.

Revision ID: b2f4d8e9a1c3
Revises: e9b5c7a3f1d8
Create Date: 2026-05-07
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b2f4d8e9a1c3"
down_revision = "e9b5c7a3f1d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent — these columns may have been hot-fixed via raw psql on
    # production already. Pure ADD COLUMN IF NOT EXISTS.
    op.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS search_vector TEXT")
    op.execute(
        "ALTER TABLE skills ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE"
    )
    # Backfill default for any existing NULLs
    op.execute("UPDATE skills SET is_archived = FALSE WHERE is_archived IS NULL")


def downgrade() -> None:
    # Reversible. The hot-fix on prod is also reversible.
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS is_archived")
    op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS search_vector")
