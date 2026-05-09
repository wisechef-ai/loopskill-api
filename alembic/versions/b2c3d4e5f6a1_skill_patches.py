"""skill_patches

Revision ID: b2c3d4e5f6a1
Revises: fb1f00d000aa
Create Date: 2026-05-09 12:00:00.000000

Creates skill_patches table for the skill-patch-as-PR MCP tool (Stream A).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b2c3d4e5f6a1"
down_revision = "fb1f00d000aa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS skill_patches (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
            api_key_h           TEXT,
            slug                TEXT,
            base_version        TEXT NOT NULL,
            dedup_hash          TEXT NOT NULL,
            file_paths_json     JSONB NOT NULL DEFAULT '[]'::jsonb,
            anon_hash           TEXT NOT NULL DEFAULT '',
            gh_pr_number        INTEGER,
            gh_pr_url           TEXT,
            status              TEXT NOT NULL DEFAULT 'pending',
            rejection_reason    TEXT,
            rationale           TEXT NOT NULL DEFAULT '',
            evidence_install_id TEXT
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_sp_api_key_h
            ON skill_patches (api_key_h)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_sp_slug
            ON skill_patches (slug)
    """)

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sp_dedup_hash
            ON skill_patches (dedup_hash)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS skill_patches")
