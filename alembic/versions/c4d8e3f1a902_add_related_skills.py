"""add_related_skills

Revision ID: c4d8e3f1a902
Revises: b8d2c5a91e3f
Create Date: 2026-04-30 15:30:00.000000

Stage 1 (G15) — Skill-graph declared edges.

Adds `skills.related_skills` JSONB column to persist the `related_skills:`
frontmatter that authors write in their SKILL.md. Backfilled by
`scripts/import_skill_metadata.py`. Surfaced via:
  - GET /api/skills/{slug}        (resolved into SkillDetailOut.related)
  - GET /api/skills/{slug}/related (dedicated public endpoint, ≤10)

ADDITIVE ONLY — per Sprint contract invariants.
  - Never DROP columns on production.
  - Default is NULL → backfill script sets [] explicitly.
  - GIN index on JSONB enables Stage 2 reverse lookups
    ("which skills name X as related?") in O(log n).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "c4d8e3f1a902"
down_revision = "b8d2c5a91e3f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Native JSONB on Postgres so we can build a GIN index for reverse
        # lookups in Stage 2 ("which skills name X as related?").
        op.add_column(
            "skills",
            sa.Column("related_skills", JSONB(), nullable=True),
        )
        op.create_index(
            "ix_skills_related_gin",
            "skills",
            ["related_skills"],
            postgresql_using="gin",
        )
    else:
        # SQLite (tests, dev): plain JSON column, no index needed
        op.add_column(
            "skills",
            sa.Column("related_skills", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.drop_index("ix_skills_related_gin", table_name="skills")
    op.drop_column("skills", "related_skills")
