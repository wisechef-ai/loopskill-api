"""add the per-owner Liked bundle primitive

Adds the marker, backfills one private Liked bundle for every existing owner,
and enforces at most one Liked bundle per owner on PostgreSQL. The upgrade is
idempotent and never changes or deletes an existing bundle.

Downgrade removes only the index and marker column. It deliberately leaves the
backfilled rows in place as ordinary bundles so rollback cannot destroy data.

Revision ID: liked0711_p0
Revises: am0706_skill_kind
Create Date: 2026-07-12
"""

from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "liked0711_p0"
down_revision = "am0706_skill_kind"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    """Return whether a table has a named column."""
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def _has_index(bind, table: str, index: str) -> bool:
    """Return whether a table has a named index."""
    return any(i["name"] == index for i in sa.inspect(bind).get_indexes(table))


def upgrade() -> None:
    """Add and backfill the Liked primitive without touching existing bundles."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    if not _has_column(bind, "bundles", "is_liked"):
        op.add_column(
            "bundles",
            sa.Column("is_liked", sa.Boolean(), nullable=False, server_default="0"),
        )

    bundles = sa.table(
        "bundles",
        sa.column("id", sa.Uuid()),
        sa.column("name"),
        sa.column("is_base"),
        sa.column("is_liked"),
        sa.column("bundle_owner", sa.Uuid()),
        sa.column("visibility"),
    )
    owners = bind.execute(
        sa.select(bundles.c.bundle_owner)
        .where(bundles.c.bundle_owner.is_not(None))
        .distinct()
    ).scalars()
    for owner_id in owners:
        liked_exists = bind.execute(
            sa.select(bundles.c.id)
            .where(
                bundles.c.bundle_owner == owner_id,
                bundles.c.is_liked.is_(True),
            )
            .limit(1)
        ).first()
        if liked_exists is None:
            bind.execute(
                bundles.insert().values(
                    id=uuid4(),
                    name="Liked",
                    is_base=False,
                    is_liked=True,
                    bundle_owner=owner_id,
                    visibility="private",
                )
            )

    if dialect == "postgresql" and not _has_index(
        bind, "bundles", "uq_bundles_is_liked_per_owner"
    ):
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_bundles_is_liked_per_owner "
            "ON bundles (bundle_owner) WHERE is_liked = true"
        )


def downgrade() -> None:
    """Remove schema objects while retaining all Liked bundle rows."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql" and _has_index(
        bind, "bundles", "uq_bundles_is_liked_per_owner"
    ):
        op.execute("DROP INDEX IF EXISTS uq_bundles_is_liked_per_owner")
    if _has_column(bind, "bundles", "is_liked"):
        op.drop_column("bundles", "is_liked")
