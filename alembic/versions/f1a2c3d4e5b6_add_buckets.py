"""add_buckets_and_bucket_skills

Revision ID: f1a2c3d4e5b6
Revises: e8f2a4d10b73
Create Date: 2026-05-01 18:00:00.000000

Phase E.1 (v5.4) — Studio buckets + white-label.

Creates two tables:
  - buckets        — owner-scoped collections of skills (Studio tier)
  - bucket_skills  — join table linking buckets to skills and/or forks

Notes on cross-branch FK to skill_forks:
  The sibling branch agent/tori/v54-forks creates `skill_forks`. To stay safe
  whether or not that table exists at apply-time we DECLARE the FK only when
  `skill_forks` is present. The column itself is NOT NULL-able, so rows can be
  inserted with fork_id=NULL even if the FK has not yet been wired up. Once
  the forks migration lands, run an alembic patch (or this migration's
  upgrade against a database where skill_forks exists) to add the constraint.

ADDITIVE ONLY — no existing column or table touched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "f1a2c3d4e5b6"
down_revision = "f3a91c5e7b4d"
branch_labels = None
depends_on = None


def _has_table(conn, name: str) -> bool:
    insp = sa.inspect(conn)
    return name in insp.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    id_type = UUID(as_uuid=True) if dialect == "postgresql" else sa.String(36)
    json_type = JSONB() if dialect == "postgresql" else sa.JSON()

    op.create_table(
        "buckets",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("owner_id", id_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "visibility",
            sa.String(32),
            nullable=False,
            server_default="private",
        ),
        sa.Column(
            "is_white_label",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("custom_domain", sa.Text(), nullable=True),
        sa.Column(
            "pin_mode",
            sa.String(32),
            nullable=False,
            server_default="latest-stable",
        ),
        sa.Column("theme_json", json_type, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "visibility IN ('private','team','public')",
            name="ck_buckets_visibility",
        ),
        sa.CheckConstraint(
            "pin_mode IN ('latest-stable','pinned-current','frozen')",
            name="ck_buckets_pin_mode",
        ),
    )
    op.create_index("ix_buckets_owner_id", "buckets", ["owner_id"])
    op.create_index(
        "ix_buckets_custom_domain",
        "buckets",
        ["custom_domain"],
        unique=False,
    )

    # bucket_skills: FK to skill_forks only when the table exists at apply-time.
    forks_present = _has_table(bind, "skill_forks")
    fork_id_args: tuple = (
        (sa.ForeignKey("skill_forks.id"),) if forks_present else ()
    )

    op.create_table(
        "bucket_skills",
        sa.Column("id", id_type, primary_key=True),
        sa.Column(
            "bucket_id",
            id_type,
            sa.ForeignKey("buckets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "skill_id",
            id_type,
            sa.ForeignKey("skills.id"),
            nullable=True,
        ),
        sa.Column("fork_id", id_type, *fork_id_args, nullable=True),
        sa.Column("version_pin", sa.String(64), nullable=True),
        sa.Column(
            "install_order",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
        sa.CheckConstraint(
            "skill_id IS NOT NULL OR fork_id IS NOT NULL",
            name="ck_bucket_skills_skill_or_fork",
        ),
    )
    op.create_index(
        "ix_bucket_skills_bucket_install_order",
        "bucket_skills",
        ["bucket_id", "install_order"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_bucket_skills_bucket_install_order",
        table_name="bucket_skills",
    )
    op.drop_table("bucket_skills")
    op.drop_index("ix_buckets_custom_domain", table_name="buckets")
    op.drop_index("ix_buckets_owner_id", table_name="buckets")
    op.drop_table("buckets")
