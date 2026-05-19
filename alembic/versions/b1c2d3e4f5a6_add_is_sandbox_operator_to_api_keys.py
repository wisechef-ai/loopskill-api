"""add_is_sandbox_operator_to_api_keys

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-05-19 22:00:00.000000

Additive migration: adds is_sandbox_operator (BOOLEAN, NOT NULL, DEFAULT FALSE)
to the api_keys table.  Safe for zero-downtime deploys — existing rows get
server_default=false so no data migration is required.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b1c2d3e4f5a6'
down_revision = 'a0b1c2d3e4f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'api_keys',
        sa.Column(
            'is_sandbox_operator',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column('api_keys', 'is_sandbox_operator')
