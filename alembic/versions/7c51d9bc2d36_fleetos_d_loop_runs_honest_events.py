"""fleetos_1607 Phase D — loop_runs honest event columns

Revision ID: 7c51d9bc2d36
Revises: a520ed06c5d2
Create Date: 2026-07-16

Phase D of fleetos_1607 — upgrade the shipped loop_runs facts table into an
HONEST registry. Adds five ADDITIVE, NULLABLE columns (existing prod rows keep
working — they predate the honest contract and are exempt from dedup):

  * tick_id          — deterministic logical firing id (dedup axis)
  * attempt          — retry counter within a tick
  * placement_epoch  — the Phase A epoch that owned the run
  * member_seq       — emitter monotonic sequence (ordering)
  * stale_epoch      — flagged runs excluded from pass numerators

Plus one dedup index. All portable (ADD COLUMN / CREATE INDEX). The stale_epoch
default is a portable CURRENT-style boolean default. No data migration — the
backfill is unnecessary (NULL tick_id rows are exempt by design).
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c51d9bc2d36"
down_revision: Union[str, Sequence[str], None] = "a520ed06c5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — additive honest-event columns on loop_runs."""
    op.add_column("loop_runs", sa.Column("tick_id", sa.String(length=255), nullable=True))
    op.add_column("loop_runs", sa.Column("attempt", sa.Integer(), nullable=True))
    op.add_column("loop_runs", sa.Column("placement_epoch", sa.Integer(), nullable=True))
    op.add_column("loop_runs", sa.Column("member_seq", sa.Integer(), nullable=True))
    op.add_column(
        "loop_runs",
        sa.Column("stale_epoch", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_index(op.f("ix_loop_runs_tick_id"), "loop_runs", ["tick_id"])
    op.create_index("ix_loop_runs_dedup", "loop_runs", ["loop_slug", "tick_id", "attempt", "placement_epoch"])


def downgrade() -> None:
    """Downgrade schema — drop the honest-event columns."""
    op.drop_index("ix_loop_runs_dedup", table_name="loop_runs")
    op.drop_index(op.f("ix_loop_runs_tick_id"), table_name="loop_runs")
    op.drop_column("loop_runs", "stale_epoch")
    op.drop_column("loop_runs", "member_seq")
    op.drop_column("loop_runs", "placement_epoch")
    op.drop_column("loop_runs", "attempt")
    op.drop_column("loop_runs", "tick_id")
