"""fleetos_1607 Phase A — loop_placements + placement_confirmations + liveness

Revision ID: ca2afa8c1bf5
Revises: 547f9f97e64d
Create Date: 2026-07-16

Phase A of fleetos_1607 — placements, the spine. Three additive tables:

  * loop_placements        — authoritative loop↔member binding, epoch-stamped,
                             CAS-guarded transitions (assigned/active/draining/
                             removed). A PARTIAL UNIQUE INDEX on Postgres enforces
                             at most ONE live (non-removed) placement per
                             (fleet_id, loop_key) — the single-active invariant at
                             the DB layer, not just the service layer.
  * placement_confirmations — old-member drain confirmations, deduped on
                             (member_id, member_seq).
  * fleet_member_liveness   — operational ping + typed provides{} for assign
                             preflight + the stale-member alert.

Portable base DDL (plain CREATE TABLE / CHECK / UNIQUE / INDEX) applies on both
Postgres and the SQLite test fixture. The ONE Postgres-only construct — the
partial unique index `WHERE status <> 'removed'` — is guarded by a dialect check
(SQLite gets the same invariant enforced in app/services/placement.py, which
never creates a second live row). No PL/pgSQL, no Postgres-only defaults.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ca2afa8c1bf5"
down_revision: Union[str, Sequence[str], None] = "547f9f97e64d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid() -> sa.types.TypeEngine:
    return postgresql.UUID(as_uuid=True).with_variant(sa.CHAR(32), "sqlite")


def upgrade() -> None:
    """Upgrade schema — three additive tables + one partial unique index."""
    op.create_table(
        "loop_placements",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("fleet_id", _uuid(), nullable=False),
        sa.Column("loop_key", sa.String(length=128), nullable=False),
        sa.Column("member_id", _uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="assigned"),
        sa.Column("placement_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_op_id", sa.String(length=64), nullable=True),
        sa.Column("forced", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["fleet_id"], ["fleets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_id"], ["fleet_members.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("fleet_id", "loop_key", "placement_epoch", name="uq_loop_placement_epoch"),
        sa.CheckConstraint(
            "status IN ('assigned','active','draining','removed')",
            name="ck_loop_placement_status",
        ),
    )
    op.create_index("idx_loop_placement_lookup", "loop_placements", ["fleet_id", "loop_key", "status"])
    op.create_index("idx_loop_placement_member", "loop_placements", ["member_id", "status"])
    op.create_index(op.f("ix_loop_placements_fleet_id"), "loop_placements", ["fleet_id"])
    op.create_index(op.f("ix_loop_placements_loop_key"), "loop_placements", ["loop_key"])
    op.create_index(op.f("ix_loop_placements_member_id"), "loop_placements", ["member_id"])

    # Postgres-only PARTIAL UNIQUE INDEX: at most one live placement per
    # (fleet, loop). This is the DB-layer teeth behind the single-active
    # invariant. SQLite can't express a partial index in alembic portably, so
    # the test fixture relies on the service layer (which never creates a second
    # live row) — the concurrency suite RED-proofs that guarantee.
    if op.get_bind().dialect.name == "postgresql":
        op.create_index(
            "uq_loop_placement_live",
            "loop_placements",
            ["fleet_id", "loop_key"],
            unique=True,
            postgresql_where=sa.text("status <> 'removed'"),
        )

    op.create_table(
        "placement_confirmations",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("placement_id", _uuid(), nullable=False),
        sa.Column("member_id", _uuid(), nullable=False),
        sa.Column("confirmed_epoch", sa.Integer(), nullable=False),
        sa.Column("member_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["placement_id"], ["loop_placements.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_id"], ["fleet_members.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("member_id", "member_seq", name="uq_placement_confirmation_seq"),
    )
    op.create_index(
        op.f("ix_placement_confirmations_placement_id"), "placement_confirmations", ["placement_id"]
    )
    op.create_index(op.f("ix_placement_confirmations_member_id"), "placement_confirmations", ["member_id"])

    op.create_table(
        "fleet_member_liveness",
        sa.Column("member_id", _uuid(), primary_key=True, nullable=False),
        sa.Column("last_ping_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("provides", sa.JSON(), nullable=False),
        sa.Column("reconcile_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.ForeignKeyConstraint(["member_id"], ["fleet_members.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_fleet_member_liveness_last_ping_at"), "fleet_member_liveness", ["last_ping_at"])


def downgrade() -> None:
    """Downgrade schema — drop the three tables."""
    op.drop_index(op.f("ix_fleet_member_liveness_last_ping_at"), table_name="fleet_member_liveness")
    op.drop_table("fleet_member_liveness")

    op.drop_index(op.f("ix_placement_confirmations_member_id"), table_name="placement_confirmations")
    op.drop_index(op.f("ix_placement_confirmations_placement_id"), table_name="placement_confirmations")
    op.drop_table("placement_confirmations")

    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("uq_loop_placement_live", table_name="loop_placements")
    op.drop_index(op.f("ix_loop_placements_member_id"), table_name="loop_placements")
    op.drop_index(op.f("ix_loop_placements_loop_key"), table_name="loop_placements")
    op.drop_index(op.f("ix_loop_placements_fleet_id"), table_name="loop_placements")
    op.drop_index("idx_loop_placement_member", table_name="loop_placements")
    op.drop_index("idx_loop_placement_lookup", table_name="loop_placements")
    op.drop_table("loop_placements")
