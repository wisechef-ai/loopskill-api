"""evergreen_0206 Phase E — reconcile_events telemetry for promotion gate

Revision ID: evergreen_e_reconcile_events
Revises: evergreen_c_promote
Create Date: 2026-06-03 00:00:00.000000

Adds the ``reconcile_events`` table. The Phase D reconcile client emits one row
per apply attempt against a (skill, version) on a channel; the Phase E promotion
engine reads canary outcomes to gate canary→stable promotion.

Postgres-only SQL.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "evergreen_e_reconcile_events"
down_revision = "evergreen_c_promote"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create reconcile_events."""
    op.create_table(
        "reconcile_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("cookbook_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("semver", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False, server_default="canary"),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            index=True,
        ),
    )
    op.create_index(
        "ix_reconcile_events_skill_semver",
        "reconcile_events",
        ["skill_id", "semver"],
    )


def downgrade() -> None:
    """Drop reconcile_events."""
    op.drop_index("ix_reconcile_events_skill_semver", table_name="reconcile_events")
    op.drop_table("reconcile_events")
