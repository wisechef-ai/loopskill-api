"""top1pct_1105 Phase C — per-cookbook API keys.

Adds two new columns to api_keys:
  label       TEXT NULL  — human label e.g. "ACME client", "data-pipeline cookbook"
  cookbook_id UUID NULL  — FK → cookbooks.id ON DELETE SET NULL (null = personal key)

No data migration needed — all existing rows get NULL for both new columns,
which is the correct default (existing keys are personal, unlabelled).

Revision ID: f2a3b4c5d6e7
Revises: e1f5a3c2b7d0 (top1pct Phase A — catalog truth)
Create Date: 2026-05-11 23:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e1f5a3c2b7d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Detect backend to handle SQLite (tests) vs Postgres (prod)
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # label column — simple VARCHAR
    op.add_column("api_keys", sa.Column("label", sa.String(100), nullable=True))

    # cookbook_id column — UUID with FK
    if is_postgres:
        op.add_column(
            "api_keys",
            sa.Column(
                "cookbook_id",
                PG_UUID(as_uuid=True),
                sa.ForeignKey("cookbooks.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    else:
        # SQLite: use plain String for UUID (no native UUID type)
        op.add_column(
            "api_keys",
            sa.Column("cookbook_id", sa.String(36), nullable=True),
        )

    # Index on cookbook_id for fast per-cookbook key lookups
    op.create_index("ix_api_keys_cookbook_id", "api_keys", ["cookbook_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_cookbook_id", table_name="api_keys")
    op.drop_column("api_keys", "cookbook_id")
    op.drop_column("api_keys", "label")
