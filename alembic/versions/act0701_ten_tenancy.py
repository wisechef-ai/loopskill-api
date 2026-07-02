"""activate_0701 Phase TEN — org tenancy + tier key caps schema

Revision ID: act0701_ten_tenancy
Revises: act0701_pt_syncrep
Create Date: 2026-07-02 18:00:00.000000

Adds org tenancy boundary:
    fleets.org_id          UUID FK orgs.id nullable, indexed (NULL = personal scope)
    bundles.org_id         UUID FK orgs.id nullable, indexed (NULL = personal scope)
    org_memberships        new table: user↔org link with role

Backfill: existing fleets/bundles get org_id = NULL (personal scope, backward compat).
"""

# revision identifiers used by Alembic.
revision = "act0701_ten_tenancy"
down_revision = "act0701_a2_cl"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ── fleets.org_id (tenant boundary) ──
    # SQLite batch mode requires named constraints; Postgres supports inline FK.
    if dialect == "sqlite":
        with op.batch_alter_table("fleets", schema=None) as batch_op:
            batch_op.add_column(sa.Column("org_id", sa.String(36), nullable=True))
    else:
        op.add_column(
            "fleets",
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("orgs.id", ondelete="SET NULL", name="fk_fleets_org_id"),
                nullable=True,
            ),
        )
    op.create_index("ix_fleets_org_id", "fleets", ["org_id"])

    # ── bundles.org_id (tenant boundary) ──
    if dialect == "sqlite":
        with op.batch_alter_table("bundles", schema=None) as batch_op:
            batch_op.add_column(sa.Column("org_id", sa.String(36), nullable=True))
    else:
        op.add_column(
            "bundles",
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("orgs.id", ondelete="SET NULL", name="fk_bundles_org_id"),
                nullable=True,
            ),
        )
    op.create_index("ix_bundles_org_id", "bundles", ["org_id"])

    # ── org_memberships table ──
    if dialect == "sqlite":
        op.create_table(
            "org_memberships",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("org_id", sa.String(36), nullable=False),
            sa.Column("user_id", sa.String(36), nullable=False),
            sa.Column("role", sa.String(32), nullable=False, server_default="member"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),
        )
    else:
        op.create_table(
            "org_memberships",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("orgs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("role", sa.String(32), nullable=False, server_default="member"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),
        )
    op.create_index("ix_org_memberships_org_id", "org_memberships", ["org_id"])
    op.create_index("ix_org_memberships_user_id", "org_memberships", ["user_id"])

    # Backfill: existing fleets/bundles get org_id = NULL (personal scope).
    # This is a no-op at the column level (nullable defaults to NULL), but
    # documented here per contract §1.


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_index("ix_org_memberships_user_id", table_name="org_memberships")
    op.drop_index("ix_org_memberships_org_id", table_name="org_memberships")
    op.drop_table("org_memberships")

    op.drop_index("ix_bundles_org_id", table_name="bundles")
    if dialect == "sqlite":
        with op.batch_alter_table("bundles", schema=None) as batch_op:
            batch_op.drop_column("org_id")
    else:
        op.drop_column("bundles", "org_id")

    op.drop_index("ix_fleets_org_id", table_name="fleets")
    if dialect == "sqlite":
        with op.batch_alter_table("fleets", schema=None) as batch_op:
            batch_op.drop_column("org_id")
    else:
        op.drop_column("fleets", "org_id")
