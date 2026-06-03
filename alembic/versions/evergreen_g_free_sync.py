"""evergreen_0206 Phase G — free-tier free_sync_used_at

Revision ID: evergreen_g_free_sync
Revises: evergreen_e_reconcile_events
Create Date: 2026-06-03 00:00:00.000000

Adds users.free_sync_used_at (nullable TIMESTAMPTZ). Stamped when a free user
runs their ONE allowed manual sync (the conversion taste). A second manual sync
returns 402/upgrade.

Postgres-only SQL.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "evergreen_g_free_sync"
down_revision = "evergreen_e_reconcile_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add free_sync_used_at to users."""
    op.add_column(
        "users",
        sa.Column("free_sync_used_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop free_sync_used_at."""
    op.drop_column("users", "free_sync_used_at")
