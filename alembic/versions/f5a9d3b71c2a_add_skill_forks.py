"""add_skill_forks

Revision ID: f5a9d3b71c2a
Revises: a7f7db696591
Create Date: 2026-05-01 12:00:00.000000

Phase D.1 — Operator-tier skill forks.

Creates two tables:
  - skill_forks       — per-user lane for editable copies of public skills
  - fork_versions     — version history for each fork (tarball + checksum)

Author-time invariants:
  - (user_id, slug) is unique — prevents collisions in a user's library
  - latest_version_id remains NULL until the first publish-fork call lands
  - Indexes on (user_id), (source_skill_id), (fork_id, created_at DESC)
    — fork_id+created_at composite supports the install endpoint's
      "latest version" lookup without a sort

ADDITIVE ONLY — no existing column or table is touched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "f5a9d3b71c2a"
down_revision = "e8f2a4d10b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    id_type = UUID(as_uuid=True) if dialect == "postgresql" else sa.String(36)

    op.create_table(
        "skill_forks",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("user_id", id_type, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("source_skill_id", id_type, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("readme", sa.Text(), nullable=True),
        sa.Column(
            "visibility",
            sa.Text(),
            server_default=sa.text("'private'"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("latest_version_id", id_type, nullable=True),
        sa.CheckConstraint(
            "visibility IS NULL OR visibility IN ('private','team','public')",
            name="ck_skill_forks_visibility",
        ),
        sa.UniqueConstraint("user_id", "slug", name="uq_skill_forks_user_slug"),
    )
    op.create_index(
        "ix_skill_forks_user", "skill_forks", ["user_id"],
    )
    op.create_index(
        "ix_skill_forks_source", "skill_forks", ["source_skill_id"],
    )

    op.create_table(
        "fork_versions",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("fork_id", id_type, sa.ForeignKey("skill_forks.id"), nullable=False),
        sa.Column("semver", sa.Text(), nullable=False),
        sa.Column("tarball_path", sa.Text(), nullable=False),
        sa.Column("tarball_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.Text(), nullable=False),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_fork_versions_fork_created",
        "fork_versions",
        ["fork_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_fork_versions_fork_created", table_name="fork_versions")
    op.drop_table("fork_versions")
    op.drop_index("ix_skill_forks_source", table_name="skill_forks")
    op.drop_index("ix_skill_forks_user", table_name="skill_forks")
    op.drop_table("skill_forks")
