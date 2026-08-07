"""mesh0408_w4 — separate self-originated loop runs from external ones

Revision ID: mesh0408_w4_synth_runs
Revises: mesh0408_t1c_extconn
Create Date: 2026-08-07

mesh_0408 W4 (Deliverable 2). LoopSkill's own ``*/3min`` proof-of-life beacon
(``p4-loop-proof``) produced 1759 of production's 1760 loop runs. Every surface
that reported "loop_runs" therefore reported LoopSkill's heartbeat as customer
adoption. This migration adds the columns that make the two numbers separable:

  * ``fleets.is_synthetic``        — the whole fleet is ours (CI / harness).
                                     NULLABLE: NULL = unclassified.
  * ``fleet_members.is_synthetic`` — this one agent is ours, in a real fleet.
                                     NULLABLE: NULL = unclassified.
  * ``loop_runs.is_synthetic``     — the frozen per-run verdict (denormalized
                                     at ingest; a run is an immutable fact)
  * ``loop_run_daily_rollups.synthetic_runs`` — the same split on the rollup,
    because raw rows are pruned at 30d and the split must survive that

...and BACKFILLS the historical runs so past numbers stop overstating, by BOTH
routes, in order:

  1. **identity** — runs from a member whose API key is ``is_test``. That
     column (spotify_0608/B) is the single definition of "this traffic is
     ours", so history is classified by the same rule live ingest uses.
  2. **the known-beacon backstop** — runs of a slug in
     ``app.services.synthetic_runs.SELF_ORIGINATED_LOOP_SLUGS`` (imported, not
     duplicated, so the two can never drift; pinned by
     ``tests/test_mesh0408_w4_synthetic_runs.py``).

The two run-count columns take a DATABASE-level default, not merely an ORM
one, so a raw INSERT that omits them lands in the external/organic state — the
conservative direction only for rows that really are external, with the beacon
backfilled explicitly below. The two IDENTITY columns deliberately take no
default at all; see ``upgrade()``.

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
    # The two identity columns are NULLABLE and default to NULL = "nobody has
    # classified this fleet/agent". That third value is load-bearing: it is the
    # ONLY state in which the SELF_ORIGINATED_LOOP_SLUGS backstop is consulted,
    # so every row this migration creates keeps reading correctly, while every
    # row a creation path writes after it carries an explicit verdict that
    # beats the slug list. Backfilling them to `false` would have declared
    # every historical fleet "a real customer's" and permanently disabled the
    # backstop on exactly the rows that need it.
    op.add_column("fleets", sa.Column("is_synthetic", sa.Boolean(), nullable=True))
    op.add_column("fleet_members", sa.Column("is_synthetic", sa.Boolean(), nullable=True))
    # The per-run verdict is NOT NULL: a run is classified once, at ingest, as
    # the immutable fact it is. A raw INSERT that omits it lands external —
    # conservative in the only direction that matters, with the known beacon
    # backfilled explicitly below.
    op.add_column(
        "loop_runs",
        sa.Column("is_synthetic", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "loop_run_daily_rollups",
        sa.Column("synthetic_runs", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )

    bind = op.get_bind()

    # ── Backfill 1: identity ────────────────────────────────────────────────
    # APIKey.is_test is the SINGLE definition of "this traffic is ours"
    # (spotify_0608/B), so history is classified by the same rule live ingest
    # uses — not by the slug list alone. Only the True direction is applied:
    # is_test is NOT NULL DEFAULT false, so its False is the column default,
    # not somebody's verdict, and writing it here would classify every fleet
    # that ever minted a key.
    bind.execute(
        sa.text(
            "UPDATE loop_runs SET is_synthetic = true WHERE member_id IN "
            "(SELECT fm.id FROM fleet_members fm "
            " JOIN api_keys ak ON ak.id = fm.api_key_id WHERE ak.is_test = true)"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE loop_run_daily_rollups SET synthetic_runs = runs WHERE member_id IN "
            "(SELECT fm.id FROM fleet_members fm "
            " JOIN api_keys ak ON ak.id = fm.api_key_id WHERE ak.is_test = true)"
        )
    )

    # ── Backfill 2: the known-beacon backstop ───────────────────────────────
    # Bound parameters (not string interpolation) so the slug set stays data,
    # and so this is safe if the set ever grows to include a customer-authored
    # name. Rollups are re-derived from the same slug set rather than from the
    # raw rows, because raw rows older than 30d have already been pruned.
    slugs = sorted(SELF_ORIGINATED_LOOP_SLUGS)
    if slugs:
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
