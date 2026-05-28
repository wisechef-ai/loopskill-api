"""topshelf_2605/H — voice-of-customer missing_skill_queries table

Revision ID: topshelf_2605_h_missing_skill_queries
Revises: topshelf_2605_d_subscriber_credits
Create Date: 2026-05-28 00:00:00.000000

Creates ``missing_skill_queries`` to capture search queries that returned
zero results. Each (lower(query), day) pair is unique; repeated zero-result
searches increment the ``count`` column so a weekly digest can surface
catalog gaps without row explosion.

Schema:
  id          UUID PK
  query       TEXT NOT NULL
  user_id     UUID nullable
  day         DATE NOT NULL
  count       INT NOT NULL DEFAULT 1
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()

Index: UNIQUE on (lower(query), day) — functional index on Postgres,
       plain unique (query, day) on SQLite (tests).

DOWNGRADE: DROP INDEX + DROP TABLE
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision = "topshelf_2605_h_missing_skill_queries"
down_revision = "topshelf_2605_d_subscriber_credits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create missing_skill_queries table with functional unique index."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Use Postgres-native UUID type on PG, Text elsewhere (SQLite tests).
    uuid_type = PG_UUID(as_uuid=True) if is_postgres else sa.Text()

    op.create_table(
        "missing_skill_queries",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("user_id", uuid_type, nullable=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column(
            "count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # Functional unique index on (lower(query), day).
    # Postgres supports expression indexes; SQLite falls back to plain unique.
    if is_postgres:
        op.execute(
            """
            CREATE UNIQUE INDEX uq_missing_skill_queries_query_day
            ON missing_skill_queries (lower(query), day)
            """
        )
    else:
        op.execute(
            """
            CREATE UNIQUE INDEX uq_missing_skill_queries_query_day
            ON missing_skill_queries (query, day)
            """
        )


def downgrade() -> None:
    """Drop missing_skill_queries table (index is dropped automatically)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS uq_missing_skill_queries_query_day")
    op.drop_table("missing_skill_queries")
