"""v7 phase J — skill_aliases table + seed chef→maestro alias

Revision ID: c7a8b9d0e1f2
Revises: b3c4d5e6f701
Create Date: 2026-05-06 12:00:00.000000

Phase J — atomic rename of the marketplace `chef` skill to `maestro`.
Adds a redirect table so `GET /api/skills/chef` continues to resolve for
90 days after the rename, then naturally fades out.

Idempotent: re-running on a DB that already has the table or alias row is
a no-op (we use IF NOT EXISTS-style guards / ON CONFLICT-equivalents that
work on both Postgres and SQLite).
"""
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from alembic import op


revision = "c7a8b9d0e1f2"
down_revision = "b3c4d5e6f701"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "skill_aliases" not in inspector.get_table_names():
        op.create_table(
            "skill_aliases",
            sa.Column("old_slug", sa.String(length=255), primary_key=True),
            sa.Column("new_slug", sa.String(length=255), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_skill_aliases_new_slug",
            "skill_aliases",
            ["new_slug"],
        )

    # Conditionally seed the chef→maestro alias row IF a chef skill exists.
    chef_exists = bind.execute(
        sa.text("SELECT 1 FROM skills WHERE slug = 'chef' LIMIT 1")
    ).first()
    alias_exists = bind.execute(
        sa.text("SELECT 1 FROM skill_aliases WHERE old_slug = 'chef' LIMIT 1")
    ).first()

    if chef_exists and not alias_exists:
        expires = datetime.now(timezone.utc) + timedelta(days=90)
        bind.execute(
            sa.text(
                "INSERT INTO skill_aliases (old_slug, new_slug, expires_at) "
                "VALUES ('chef', 'maestro', :exp)"
            ),
            {"exp": expires},
        )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_aliases" in inspector.get_table_names():
        op.drop_index("ix_skill_aliases_new_slug", table_name="skill_aliases")
        op.drop_table("skill_aliases")
