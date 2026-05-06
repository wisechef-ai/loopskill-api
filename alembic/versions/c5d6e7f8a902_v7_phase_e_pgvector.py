"""v7 phase E — pgvector embedding column

Revision ID: c5d6e7f8a902
Revises: b3c4d5e6f701
Create Date: 2026-05-06 12:00:00.000000

Phase E — adds an `embedding` column to the `skills` table to power the
hybrid `/api/recall` endpoint.

Dialect-aware: on Postgres we install the `pgvector` extension and use
`vector(384)`; on SQLite we use TEXT (JSON-encoded list of 384 floats).
"""
from alembic import op
import sqlalchemy as sa


revision = "c5d6e7f8a902"
down_revision = "b3c4d5e6f701"
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _has_column(conn, table: str, column: str) -> bool:
    insp = sa.inspect(conn)
    try:
        return any(c["name"] == column for c in insp.get_columns(table))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    dialect = _dialect()

    if _has_column(bind, "skills", "embedding"):
        return

    if dialect == "postgresql":
        # Best-effort: extension may already be installed by the operator.
        try:
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception:
            pass
        # If pgvector is available, use vector(384). Otherwise fall back to TEXT
        # so the migration still applies cleanly (and the app stores JSON).
        try:
            op.execute("ALTER TABLE skills ADD COLUMN embedding vector(384)")
        except Exception:
            op.add_column(
                "skills",
                sa.Column("embedding", sa.Text(), nullable=True),
            )
    else:
        op.add_column(
            "skills",
            sa.Column("embedding", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "skills", "embedding"):
        return
    op.drop_column("skills", "embedding")
