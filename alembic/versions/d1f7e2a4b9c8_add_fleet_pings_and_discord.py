"""add_fleet_pings_and_discord_columns

Revision ID: d1f7e2a4b9c8
Revises: f1a2c3d4e5b6
Create Date: 2026-05-03 17:45:00.000000

Phase D (stabilization_2605) — fleet heartbeat + Discord linking.

ADDITIVE ONLY:
  - new table `fleet_pings(id, salt_hash, last_seen_day, created_at)` with
    a unique index on (salt_hash, last_seen_day) for idempotency.
  - new view `weekly_fleet_active_v` (Postgres only) — distinct salt_hashes
    per ISO week. Loaded into Grafana via the JSON in
    `devops/grafana/recipes-fleet-weekly.json`.
  - two new nullable columns on `users`: `discord_user_id`,
    `creator_track_record_score`.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "d1f7e2a4b9c8"
down_revision = "f1a2c3d4e5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    id_type = UUID(as_uuid=True) if dialect == "postgresql" else sa.String(36)
    bytea_type = sa.LargeBinary() if dialect != "postgresql" else sa.dialects.postgresql.BYTEA()

    op.create_table(
        "fleet_pings",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("salt_hash", bytea_type, nullable=False),
        sa.Column("last_seen_day", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_fleet_pings_salt_hash",
        "fleet_pings",
        ["salt_hash"],
    )
    op.create_index(
        "ix_fleet_pings_last_seen_day",
        "fleet_pings",
        ["last_seen_day"],
    )
    # Unique constraint on the newly-created table.  Postgres supports
    # ALTER TABLE ADD CONSTRAINT; SQLite does not, so we skip it there.
    # The constraint is effectively enforced by the application-level
    # deduplication logic on SQLite (which is local dev only).
    if dialect == "postgresql":
        op.create_unique_constraint(
            "uq_fleet_pings_hash_day",
            "fleet_pings",
            ["salt_hash", "last_seen_day"],
        )

    # Postgres-only aggregate view (NO drill-down columns by construction).
    if dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE VIEW weekly_fleet_active_v AS
            SELECT
              to_char(last_seen_day, 'IYYY-"W"IW') AS week,
              COUNT(DISTINCT salt_hash)            AS active_count
            FROM fleet_pings
            GROUP BY to_char(last_seen_day, 'IYYY-"W"IW')
            ORDER BY week
            """
        )

    # Discord + creator score on users (additive, nullable)
    op.add_column(
        "users",
        sa.Column("discord_user_id", sa.String(32), nullable=True),
    )
    op.create_index(
        "ix_users_discord_user_id",
        "users",
        ["discord_user_id"],
    )
    op.add_column(
        "users",
        sa.Column("creator_track_record_score", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_index("ix_users_discord_user_id", table_name="users")
    op.drop_column("users", "creator_track_record_score")
    op.drop_column("users", "discord_user_id")

    if dialect == "postgresql":
        op.execute("DROP VIEW IF EXISTS weekly_fleet_active_v")

    if dialect == "postgresql":
        op.drop_constraint("uq_fleet_pings_hash_day", "fleet_pings", type_="unique")
    op.drop_index("ix_fleet_pings_last_seen_day", table_name="fleet_pings")
    op.drop_index("ix_fleet_pings_salt_hash", table_name="fleet_pings")
    op.drop_table("fleet_pings")
