"""coldstart_0609/A — is_probe on missing_skill_queries + install_events.

Revision ID: coldstart0609_is_probe
Revises: flywheel0902_funnel_ledger
Create Date: 2026-09-06 00:00:00.000000

Adds a non-null ``is_probe`` boolean (default False) to both
``missing_skill_queries`` and ``install_events`` so demand/install
analytics can exclude the fleet's own known probe traffic (known
fleet-system x-api-key owners, or a fixed set of known probe/loopback
client IPs — see app/services/probe_detection.py, the ONE function both
writers call to decide the value).

Postgres-and-sqlite safe: ``server_default=sa.false()`` backfills every
existing row at DDL time on both dialects (Postgres literal ``false``,
SQLite ``0``), so the column can be added NOT NULL in one step with no
separate backfill/ALTER-to-NOT-NULL dance.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "coldstart0609_is_probe"
down_revision = "flywheel0902_funnel_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "missing_skill_queries",
        sa.Column("is_probe", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "install_events",
        sa.Column("is_probe", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("install_events", "is_probe")
    op.drop_column("missing_skill_queries", "is_probe")
