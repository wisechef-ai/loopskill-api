"""fix — member_lockfile_snapshots id columns must be UUID, not VARCHAR(36).

fc0706_lockfile_snap declared id/member_id/fleet_id as sa.String(36) while the
ORM model uses UUID(as_uuid=True). SQLite tests can't catch this (ORM
create_all, stringly-typed); Postgres throws `operator does not exist:
character varying = uuid` on the first snapshot upsert — every sync-report
carrying lockfile_state 500'd. Cast in place (table is brand-new/empty in
prod, but USING keeps this safe even with rows).

Revision ID: fc0706b_snap_uuid
Revises: fc0706_lockfile_snap
"""

revision = "fc0706b_snap_uuid"
down_revision = "fc0706_lockfile_snap"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (tests) has no UUID type — the ORM layer handles it there.
        return
    for col in ("id", "member_id", "fleet_id"):
        op.alter_column(
            "member_lockfile_snapshots",
            col,
            type_=postgresql.UUID(as_uuid=True),
            postgresql_using=f"{col}::uuid",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for col in ("id", "member_id", "fleet_id"):
        op.alter_column(
            "member_lockfile_snapshots",
            col,
            type_=sa.String(36),
            postgresql_using=f"{col}::text",
        )
