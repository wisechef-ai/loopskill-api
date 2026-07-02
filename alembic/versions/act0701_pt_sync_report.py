"""activate_0701 Phase T — loop_runs, cron_health_snapshots, skill_error_reports, loop_run_daily_rollups

Revision ID: act0701_pt_syncrep
Revises: act0701_p1_members
Create Date: 2026-07-02 12:00:00.000000

Creates the four sync-report tables for batched fleet telemetry ingestion.
All sections optional — an empty cycle is a heartbeat-shaped no-op.

Tables:
    loop_runs               raw loop outcome records (30d retention)
    cron_health_snapshots   per-member per-cycle cron failures + counts (30d)
    skill_error_reports     agent-reported skill errors (FB phase consumes pending)
    loop_run_daily_rollups  daily aggregate per (fleet, member, loop_slug, day) — kept indefinitely
"""

# revision identifiers used by Alembic.
revision = "act0701_pt_syncrep"
down_revision = "act0701_p1_members"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402


def upgrade() -> None:
    op.create_table(
        "loop_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fleet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("loop_slug", sa.String(255), nullable=False),
        sa.Column("instance_key", sa.String(255), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("accepted_change", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("provenance_id", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_loop_runs_member_id", "loop_runs", ["member_id"])
    op.create_index("ix_loop_runs_fleet_id", "loop_runs", ["fleet_id"])
    op.create_index("ix_loop_runs_loop_slug", "loop_runs", ["loop_slug"])
    op.create_index("ix_loop_runs_provenance_id", "loop_runs", ["provenance_id"])
    op.create_index("ix_loop_runs_created_at", "loop_runs", ["created_at"])
    op.create_index(
        "ix_loop_runs_member_slug_created",
        "loop_runs",
        ["member_id", "loop_slug", "created_at"],
    )

    op.create_table(
        "cron_health_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fleet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("failed", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("ok_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_cron_health_snapshots_member_id", "cron_health_snapshots", ["member_id"])
    op.create_index("ix_cron_health_snapshots_fleet_id", "cron_health_snapshots", ["fleet_id"])
    op.create_index("ix_cron_health_snapshots_created_at", "cron_health_snapshots", ["created_at"])

    op.create_table(
        "skill_error_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fleet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("semver", sa.String(32), nullable=True),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("feedback_status", sa.Text(), server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_skill_error_reports_member_id", "skill_error_reports", ["member_id"])
    op.create_index("ix_skill_error_reports_fleet_id", "skill_error_reports", ["fleet_id"])
    op.create_index("ix_skill_error_reports_slug", "skill_error_reports", ["slug"])
    op.create_index("ix_skill_error_reports_created_at", "skill_error_reports", ["created_at"])

    op.create_table(
        "loop_run_daily_rollups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("fleet_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("loop_slug", sa.String(255), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("runs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("successes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_changes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd_total", sa.Numeric(12, 4), nullable=True),
        sa.Column("duration_seconds_total", sa.BigInteger(), nullable=True),
        sa.UniqueConstraint("fleet_id", "member_id", "loop_slug", "day", name="uq_loop_rollup"),
    )
    op.create_index("ix_loop_run_daily_rollups_fleet_id", "loop_run_daily_rollups", ["fleet_id"])


def downgrade() -> None:
    op.drop_index("ix_loop_run_daily_rollups_fleet_id", table_name="loop_run_daily_rollups")
    op.drop_table("loop_run_daily_rollups")

    op.drop_index("ix_skill_error_reports_created_at", table_name="skill_error_reports")
    op.drop_index("ix_skill_error_reports_slug", table_name="skill_error_reports")
    op.drop_index("ix_skill_error_reports_fleet_id", table_name="skill_error_reports")
    op.drop_index("ix_skill_error_reports_member_id", table_name="skill_error_reports")
    op.drop_table("skill_error_reports")

    op.drop_index("ix_cron_health_snapshots_created_at", table_name="cron_health_snapshots")
    op.drop_index("ix_cron_health_snapshots_fleet_id", table_name="cron_health_snapshots")
    op.drop_index("ix_cron_health_snapshots_member_id", table_name="cron_health_snapshots")
    op.drop_table("cron_health_snapshots")

    op.drop_index("ix_loop_runs_member_slug_created", table_name="loop_runs")
    op.drop_index("ix_loop_runs_created_at", table_name="loop_runs")
    op.drop_index("ix_loop_runs_provenance_id", table_name="loop_runs")
    op.drop_index("ix_loop_runs_loop_slug", table_name="loop_runs")
    op.drop_index("ix_loop_runs_fleet_id", table_name="loop_runs")
    op.drop_index("ix_loop_runs_member_id", table_name="loop_runs")
    op.drop_table("loop_runs")
