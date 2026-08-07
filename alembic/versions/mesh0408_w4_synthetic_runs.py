"""mesh0408_w4 — separate self-originated loop runs from external ones

Revision ID: mesh0408_w4_synth_runs
Revises: mesh0408_t1c_extconn
Create Date: 2026-08-07

mesh_0408 W4 (Deliverable 2). LoopSkill's own ``*/3min`` proof-of-life beacon
(``p4-loop-proof``) produced 1759 of production's 1760 loop runs. Every surface
that reported "loop_runs" therefore reported LoopSkill's heartbeat as customer
adoption. This migration adds the columns that make the two numbers separable:

  * ``fleets.is_synthetic``        — the whole fleet is ours (CI / harness)
  * ``fleet_members.is_synthetic`` — this one agent is ours, in a real fleet
  * ``loop_runs.is_synthetic``     — the frozen per-run verdict (denormalized
                                     at ingest; a run is an immutable fact)
  * ``loop_run_daily_rollups.synthetic_runs`` — the same split on the rollup,
    because raw rows are pruned at 30d and the split must survive that

...and BACKFILLS the historical beacon rows so past numbers stop overstating.
The backfill seeds from ``app.services.synthetic_runs.SELF_ORIGINATED_LOOP_SLUGS``
(kept in sync by ``tests/test_mesh0408_w4_synthetic_runs.py``) rather than
duplicating a literal that could drift away from the application's definition.

All four columns take ``server_default 'false'`` / ``'0'`` at the DATABASE
level, not merely in the ORM, so a raw INSERT that omits them lands in the
external/organic state — the conservative direction only for rows that really
are external, with the beacon backfilled explicitly below.

DOWNGRADE: drop the four columns. The backfill is not reversible (the
information lives only in these columns), which is fine — dropping them
restores exactly the pre-W4 state where nothing was distinguishable.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

from app.services.synthetic_runs import SELF_ORIGINATED_LOOP_SLUGS

# revision identifiers, used by Alembic.
revision: str = "mesh0408_w4_synth_runs"
down_revision: Union[str, Sequence[str], None] = "mesh0408_t1c_extconn"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "fleets",
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "fleet_members",
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "loop_runs",
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "loop_run_daily_rollups",
        sa.Column("synthetic_runs", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    # ── Backfill: historical self-originated runs ───────────────────────────
    # Bound parameters (not string interpolation) so the slug set stays data,
    # and so this is safe if the set ever grows to include a customer-authored
    # name. Rollups are re-derived from the same slug set rather than from the
    # raw rows, because raw rows older than 30d have already been pruned.
    slugs = sorted(SELF_ORIGINATED_LOOP_SLUGS)
    if slugs:
        bind = op.get_bind()
        bind.execute(
            sa.text("UPDATE loop_runs SET is_synthetic = true WHERE loop_slug IN :slugs").bindparams(
                sa.bindparam("slugs", value=slugs, expanding=True)
            )
        )
        bind.execute(
            sa.text(
                "UPDATE loop_run_daily_rollups SET synthetic_runs = runs WHERE loop_slug IN :slugs"
            ).bindparams(sa.bindparam("slugs", value=slugs, expanding=True))
        )


def downgrade() -> None:
    op.drop_column("loop_run_daily_rollups", "synthetic_runs")
    op.drop_column("loop_runs", "is_synthetic")
    op.drop_column("fleet_members", "is_synthetic")
    op.drop_column("fleets", "is_synthetic")
