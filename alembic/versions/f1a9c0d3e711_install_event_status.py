"""install_event_status

Revision ID: f1a9c0d3e711
Revises: e8f2a4d10b73
Create Date: 2026-05-01 12:00:00.000000

Phase F.6 — Atomic rollback. Add ``status`` column to ``install_events`` so
the rollback path can record ``status='rolled_back'`` on partial failures.

ADDITIVE ONLY. Default value 'ok' for back-compat with existing rows.
"""
from alembic import op
import sqlalchemy as sa


revision = "f1a9c0d3e711"
down_revision = "e8f2a4d10b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("install_events") as batch:
        batch.add_column(
            sa.Column("status", sa.String(length=32), nullable=False,
                      server_default="ok")
        )
    op.create_index("ix_install_events_status", "install_events", ["status"])


def downgrade() -> None:
    op.drop_index("ix_install_events_status", table_name="install_events")
    with op.batch_alter_table("install_events") as batch:
        batch.drop_column("status")
