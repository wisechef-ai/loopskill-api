"""feedback_v1_tables

Revision ID: a1b2c3d4e5f6
Revises: 0d8c25489899
Create Date: 2026-05-08 18:00:00.000000

Creates recipify_requests and feedback_submissions tables for Stream 1
of the recipes-feedback-loop sprint.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "0d8c25489899"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── recipify_requests ────────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS recipify_requests (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            target_name      TEXT NOT NULL,
            why_useful       TEXT NOT NULL,
            suggested_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
            agent_id         TEXT,
            api_key_id       UUID,
            signature        TEXT NOT NULL,
            issue_url        TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_rr_api_key_created
            ON recipify_requests (api_key_id, created_at DESC)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_rr_signature
            ON recipify_requests (signature)
    """)

    # ── feedback_submissions ─────────────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS feedback_submissions (
            id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            category     TEXT NOT NULL,
            message      TEXT NOT NULL,
            context      JSONB NOT NULL DEFAULT '{}'::jsonb,
            agent_id     TEXT,
            api_key_id   UUID,
            signature    TEXT NOT NULL,
            issue_url    TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_fs_api_key_created
            ON feedback_submissions (api_key_id, created_at DESC)
    """)

    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_fs_signature
            ON feedback_submissions (signature)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feedback_submissions")
    op.execute("DROP TABLE IF EXISTS recipify_requests")
