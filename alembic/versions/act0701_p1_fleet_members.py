"""activate_0701 phase 1 — FleetMember + ReconcileEvent.member_id

Revision ID: act0701_p1_members
Revises: lsk0627_loop_feedback
Create Date: 2026-07-02 00:00:00.000000

Product lock #13: the per-agent API key IS the member identity.
One key = one agent = one FleetMember. ReconcileEvent gains member_id
so all future reports carry member identity.

Creates ``fleet_members``:
    id              UUID PK
    fleet_id        UUID FK fleets.id CASCADE
    host            VARCHAR(255) NOT NULL
    profile         VARCHAR(100) NOT NULL DEFAULT 'default'
    skills_dir      TEXT NOT NULL
    api_key_id      UUID FK api_keys.id UNIQUE (one key = one member)
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    UNIQUE(fleet_id, host, profile)

Adds to ``reconcile_events``:
    member_id       UUID nullable, indexed (no FK — events must survive member deletion)
"""

# revision identifiers used by Alembic.
revision = "act0701_p1_members"
down_revision = "lsk0627_loop_feedback"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402


def upgrade() -> None:
    op.create_table(
        "fleet_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "fleet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fleets.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("profile", sa.String(100), nullable=False, server_default="default"),
        sa.Column("skills_dir", sa.Text(), nullable=False),
        sa.Column(
            "api_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_keys.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("fleet_id", "host", "profile", name="uq_fleet_members_fleet_host_profile"),
    )

    op.add_column(
        "reconcile_events",
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_reconcile_events_member_id", "reconcile_events", ["member_id"])


def downgrade() -> None:
    op.drop_index("ix_reconcile_events_member_id", table_name="reconcile_events")
    op.drop_column("reconcile_events", "member_id")
    op.drop_table("fleet_members")
