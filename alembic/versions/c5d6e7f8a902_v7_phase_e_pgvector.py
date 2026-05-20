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


def _pgvector_available(conn) -> bool:
    """Check whether the pgvector extension is installable WITHOUT trying.

    Trying ``CREATE EXTENSION vector`` inside the alembic transaction and
    swallowing the error leaves the Postgres transaction in an aborted
    state — the NEXT statement then fails with ``InFailedSqlTransaction``,
    which is exactly the regression that bit us during the recipes_2005
    Phase K verification run against a vanilla ``postgres:16`` (no pgvector)
    container. Instead, ask the catalog UP-FRONT whether the extension
    SHARED LIBRARY is present and let the transaction stay clean.
    """
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    ).scalar()
    return result is not None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = _dialect()

    if _has_column(bind, "skills", "embedding"):
        return

    if dialect == "postgresql":
        if _pgvector_available(bind):
            # pgvector is shipped (e.g. pgvector/pgvector:pgN image, or it
            # was installed on the host via apt/yum). Safe to CREATE EXT +
            # use the native vector type — no try/except needed.
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
            op.execute("ALTER TABLE skills ADD COLUMN embedding vector(384)")
        else:
            # Vanilla Postgres without pgvector. Fall back to TEXT so the
            # migration applies cleanly; the app falls back to JSON-encoded
            # embeddings at runtime.
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
