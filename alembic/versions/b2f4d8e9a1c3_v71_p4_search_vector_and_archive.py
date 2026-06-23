"""v71_p4_search_vector_and_archive

Adds the columns Phase 4 (BM25 reindex) introduced in the Skill model but
forgot to migrate. Idempotent (IF-NOT-EXISTS guards) so it's safe
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


def _has_column(bind, table: str, col: str) -> bool:
    return any(c["name"] == col for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        # Postgres supports ADD COLUMN IF NOT EXISTS natively
        op.execute("ALTER TABLE skills ADD COLUMN IF NOT EXISTS search_vector TEXT")
        op.execute(
            "ALTER TABLE skills ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE"
        )
    else:
        # SQLite: check via inspector to get IF-NOT-EXISTS semantics
        if not _has_column(bind, "skills", "search_vector"):
            op.add_column("skills", sa.Column("search_vector", sa.Text(), nullable=True))
        if not _has_column(bind, "skills", "is_archived"):
            op.add_column(
                "skills",
                sa.Column("is_archived", sa.Boolean(), nullable=False,
                          server_default=sa.false()),
            )

    op.execute("UPDATE skills SET is_archived = FALSE WHERE is_archived IS NULL")


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS is_archived")
        op.execute("ALTER TABLE skills DROP COLUMN IF EXISTS search_vector")
    else:
        with op.batch_alter_table("skills") as batch_op:
            if _has_column(bind, "skills", "is_archived"):
                batch_op.drop_column("is_archived")
            if _has_column(bind, "skills", "search_vector"):
                batch_op.drop_column("search_vector")
