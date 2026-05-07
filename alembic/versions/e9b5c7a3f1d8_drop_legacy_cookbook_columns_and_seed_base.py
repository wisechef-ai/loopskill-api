"""drop legacy cookbook columns + seed base cookbook

Drops two legacy columns from the cookbooks table that predate v7:
  * owner_fleet_id (NOT NULL, blocked v7 cookbook creation)
  * shared_key_hash (replaced by cookbook_link_token in v7)

Adds a unique partial index on is_base=true so only ONE base cookbook ever
exists per deployment.

Seeds the base cookbook ("WiseChef Recipes Catalog") on upgrade so personal
cookbooks have a parent to fork from.

This is an idempotent recovery migration that brings any deployment that was
stamped on a pre-v7 schema (like the production VPS we cut over from
wiserecipes-api.git on 2026-05-07) back to v7-compatible state.

Revision ID: e9b5c7a3f1d8
Revises: d8a4b1c2e9f3
Create Date: 2026-05-07
"""

from alembic import op
import sqlalchemy as sa


revision = "e9b5c7a3f1d8"
down_revision = "d8a4b1c2e9f3"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(c["name"] == column for c in insp.get_columns(table))
    except Exception:
        return False


def _has_index(bind, table: str, index: str) -> bool:
    insp = sa.inspect(bind)
    try:
        return any(i["name"] == index for i in insp.get_indexes(table))
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1) Drop legacy columns (only if they exist — fresh installs won't have them)
    if _has_column(bind, "cookbooks", "owner_fleet_id"):
        op.execute("ALTER TABLE cookbooks DROP COLUMN IF EXISTS owner_fleet_id CASCADE")
    if _has_column(bind, "cookbooks", "shared_key_hash"):
        op.execute("ALTER TABLE cookbooks DROP COLUMN IF EXISTS shared_key_hash CASCADE")

    # 2) Unique partial index (only one base cookbook ever)
    # SQLite doesn't support partial indexes pre-3.8, but our test infra is on 3.40+,
    # so we just guard with the helper.
    if dialect == "postgresql" and not _has_index(bind, "cookbooks", "uq_cookbooks_is_base_true"):
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_cookbooks_is_base_true "
            "ON cookbooks (is_base) WHERE is_base = true"
        )

    # 3) Seed base cookbook + populate with all public skills
    if dialect == "postgresql":
        op.execute(
            """
            INSERT INTO cookbooks (id, name, description, is_base, created_at, updated_at)
            SELECT gen_random_uuid(),
                   'WiseChef Recipes Catalog',
                   'The official catalog of all skills published by WiseChef. '
                   'Every personal cookbook forks from this one.',
                   true,
                   NOW(),
                   NOW()
            WHERE NOT EXISTS (SELECT 1 FROM cookbooks WHERE is_base = true)
            """
        )
        # Populate base cookbook with all public skills (idempotent via ON CONFLICT)
        op.execute(
            """
            INSERT INTO cookbook_skills (cookbook_id, skill_id, source, added_at)
            SELECT cb.id, s.id, 'forked', NOW()
            FROM cookbooks cb
            CROSS JOIN skills s
            WHERE cb.is_base = true
              AND s.is_public = true
            ON CONFLICT DO NOTHING
            """
        )


def downgrade() -> None:
    """Best-effort downgrade. Re-adds legacy columns as NULLable.

    We do NOT re-create the NOT NULL constraint on owner_fleet_id because
    we have no way to backfill it without losing customer data.
    """
    bind = op.get_bind()
    if not _has_column(bind, "cookbooks", "owner_fleet_id"):
        op.add_column(
            "cookbooks",
            sa.Column("owner_fleet_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        )
    if not _has_column(bind, "cookbooks", "shared_key_hash"):
        op.add_column(
            "cookbooks",
            sa.Column("shared_key_hash", sa.String(length=255), nullable=True),
        )
