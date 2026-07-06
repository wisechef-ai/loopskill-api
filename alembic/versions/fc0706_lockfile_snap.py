"""feat/fleet-console-state — member lockfile snapshots (latest actual state).

One row per FleetMember, upserted on each sync-report. Answers "what is
actually installed on this agent right now" — the foundation of the fleet
console (installed / drift / undeclared-extras views).

Revision ID: fc0706_lockfile_snap
Revises: act0701_e_residency
"""

revision = "fc0706_lockfile_snap"
down_revision = "act0701_e_residency"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
import sqlalchemy as sa  # noqa: E402


def upgrade() -> None:
    op.create_table(
        "member_lockfile_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("member_id", sa.String(36), nullable=False, unique=True, index=True),
        sa.Column("fleet_id", sa.String(36), nullable=False, index=True),
        sa.Column("skills", sa.JSON(), nullable=False),
        sa.Column("cycle_ts", sa.String(64), nullable=True),
        sa.Column(
            "reported_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("member_lockfile_snapshots")
