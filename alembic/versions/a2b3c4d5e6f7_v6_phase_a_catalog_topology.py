"""v6_phase_a_catalog_topology

Revision ID: a2b3c4d5e6f7
Revises: f1a2c3d4e5b6
Create Date: 2026-05-04 00:00:00.000000

Phase A — catalog topology: skill_variant columns, cookbooks, cookbook_skills,
fleets, fleet_subscriptions.

ADDITIVE ONLY — uses IF NOT EXISTS guards on all ADD COLUMN and CREATE TABLE ops.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID

revision = "a2b3c4d5e6f7"
down_revision = "7da7d5cc45e8"
branch_labels = None
depends_on = None


def _dialect() -> str:
    return op.get_bind().dialect.name


def _uuid_type():
    if _dialect() == "postgresql":
        return PG_UUID(as_uuid=True)
    return sa.String(36)


def _json_type():
    if _dialect() == "postgresql":
        return JSONB()
    return sa.JSON()


def _has_column(conn, table: str, column: str) -> bool:
    insp = sa.inspect(conn)
    return any(c["name"] == column for c in insp.get_columns(table))


def _has_table(conn, name: str) -> bool:
    insp = sa.inspect(conn)
    return name in insp.get_table_names()


def _add_column_if_missing(conn, table: str, column: sa.Column) -> None:
    if not _has_column(conn, table, column.key):
        op.add_column(table, column)


def upgrade() -> None:
    conn = op.get_bind()
    id_type = _uuid_type()
    json_type = _json_type()

    # ── skills table — new variant / catalog columns ─────────────────────
    _add_column_if_missing(conn, "skills", sa.Column(
        "skill_variant", sa.String(20), nullable=False, server_default="custom"
    ))
    _add_column_if_missing(conn, "skills", sa.Column(
        "original_source_url", sa.Text, nullable=True
    ))
    _add_column_if_missing(conn, "skills", sa.Column(
        "parent_skill_slug", sa.String(255), nullable=True
    ))
    _add_column_if_missing(conn, "skills", sa.Column(
        "pinned_sha", sa.String(64), nullable=True
    ))
    _add_column_if_missing(conn, "skills", sa.Column(
        "upstream_status", sa.String(20), nullable=False, server_default="active"
    ))
    _add_column_if_missing(conn, "skills", sa.Column(
        "external_resources", json_type, nullable=True
    ))

    if _dialect() == "postgresql":
        # Partial index on upstream_status for quick non-active lookups
        op.execute("""
            CREATE INDEX IF NOT EXISTS idx_skills_variant
            ON skills(skill_variant)
        """)
        op.execute("""
            CREATE INDEX IF NOT EXISTS idx_skills_upstream_status
            ON skills(upstream_status) WHERE upstream_status != 'active'
        """)

    # ── cookbooks ────────────────────────────────────────────────────────
    if not _has_table(conn, "cookbooks"):
        op.create_table(
            "cookbooks",
            sa.Column("id", id_type, primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("is_base", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("parent_cookbook_id", id_type, nullable=True),
            sa.Column("cookbook_owner", id_type, nullable=True),
            sa.Column("cookbook_link_token", sa.String(64), nullable=True),
            sa.Column("link_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("synced_from_cookbook_id", id_type, nullable=True),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False
            ),
        )
        op.create_index("idx_cookbooks_owner", "cookbooks", ["cookbook_owner"])
        op.create_index("idx_cookbooks_parent", "cookbooks", ["parent_cookbook_id"])

        if _dialect() == "postgresql":
            op.execute("""
                CREATE UNIQUE INDEX idx_cookbooks_one_base
                ON cookbooks(is_base) WHERE is_base = TRUE
            """)

    # ── cookbook_skills ──────────────────────────────────────────────────
    if not _has_table(conn, "cookbook_skills"):
        op.create_table(
            "cookbook_skills",
            sa.Column("cookbook_id", id_type, nullable=False),
            sa.Column("skill_id", id_type, nullable=False),
            sa.Column("source", sa.String(20), nullable=False),
            sa.Column("pinned_version", sa.String(50), nullable=True),
            sa.Column(
                "added_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False
            ),
            sa.PrimaryKeyConstraint("cookbook_id", "skill_id", name="pk_cookbook_skills"),
        )
        op.create_index("idx_cookbook_skills_source", "cookbook_skills", ["source"])

    # ── fleets ───────────────────────────────────────────────────────────
    if not _has_table(conn, "fleets"):
        op.create_table(
            "fleets",
            sa.Column("id", id_type, primary_key=True),
            sa.Column("owner_user_id", id_type, nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column(
                "fleet_api_key_hash", sa.String(64),
                nullable=False, unique=True
            ),
            sa.Column(
                "created_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False
            ),
        )

    # ── fleet_subscriptions ──────────────────────────────────────────────
    if not _has_table(conn, "fleet_subscriptions"):
        op.create_table(
            "fleet_subscriptions",
            sa.Column("fleet_id", id_type, nullable=False),
            sa.Column("cookbook_id", id_type, nullable=False),
            sa.Column(
                "channel", sa.String(20),
                nullable=False, server_default="stable"
            ),
            sa.Column(
                "subscribed_at", sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False
            ),
            sa.PrimaryKeyConstraint(
                "fleet_id", "cookbook_id", name="pk_fleet_subscriptions"
            ),
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _has_table(conn, "fleet_subscriptions"):
        op.drop_table("fleet_subscriptions")
    if _has_table(conn, "fleets"):
        op.drop_table("fleets")
    if _has_table(conn, "cookbook_skills"):
        op.drop_index("idx_cookbook_skills_source", table_name="cookbook_skills")
        op.drop_table("cookbook_skills")
    if _has_table(conn, "cookbooks"):
        op.drop_index("idx_cookbooks_parent", table_name="cookbooks")
        op.drop_index("idx_cookbooks_owner", table_name="cookbooks")
        op.drop_table("cookbooks")

    # Note: ADD COLUMN is not easily reversible in SQLite. Postgres supports
    # DROP COLUMN; skip for SQLite compatibility in tests.
    if _dialect() == "postgresql":
        for col in [
            "external_resources", "upstream_status", "pinned_sha",
            "parent_skill_slug", "original_source_url", "skill_variant",
        ]:
            op.drop_column("skills", col)
