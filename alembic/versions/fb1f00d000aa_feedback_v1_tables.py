"""feedback_v1_tables

Revision ID: fb1f00d000aa
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
revision = "fb1f00d000aa"
down_revision = "0d8c25489899"
branch_labels = None
depends_on = None


def _uuid_pk(is_pg: bool) -> sa.Column:
    col_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)
    return sa.Column("id", col_type, primary_key=True, nullable=False)


def _uuid_col(name: str, is_pg: bool, *, nullable: bool = True) -> sa.Column:
    col_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)
    return sa.Column(name, col_type, nullable=nullable)


def _json_type(is_pg: bool) -> sa.types.TypeEngine:
    return postgresql.JSONB() if is_pg else sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    existing = set(sa.inspect(bind).get_table_names())
    json_type = _json_type(is_pg)

    # ── recipify_requests ─────────────────────────────────────────────────
    if "recipify_requests" not in existing:
        op.create_table(
            "recipify_requests",
            _uuid_pk(is_pg),
            sa.Column("target_name", sa.Text(), nullable=False),
            sa.Column("why_useful", sa.Text(), nullable=False),
            sa.Column("suggested_sources", json_type, nullable=False,
                      server_default="[]"),
            sa.Column("agent_id", sa.Text(), nullable=True),
            _uuid_col("api_key_id", is_pg, nullable=True),
            sa.Column("signature", sa.Text(), nullable=False),
            sa.Column("issue_url", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("idx_rr_api_key_created", "recipify_requests",
                        ["api_key_id", "created_at"])
        op.create_index("idx_rr_signature", "recipify_requests", ["signature"])

    # ── feedback_submissions ──────────────────────────────────────────────
    if "feedback_submissions" not in existing:
        op.create_table(
            "feedback_submissions",
            _uuid_pk(is_pg),
            sa.Column("category", sa.Text(), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("context", json_type, nullable=False,
                      server_default="{}"),
            sa.Column("agent_id", sa.Text(), nullable=True),
            _uuid_col("api_key_id", is_pg, nullable=True),
            sa.Column("signature", sa.Text(), nullable=False),
            sa.Column("issue_url", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index("idx_fs_api_key_created", "feedback_submissions",
                        ["api_key_id", "created_at"])
        op.create_index("idx_fs_signature", "feedback_submissions", ["signature"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS feedback_submissions")
    op.execute("DROP TABLE IF EXISTS recipify_requests")
