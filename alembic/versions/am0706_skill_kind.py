"""feat(artifact-kind-phase1) — add `kind` discriminator + `loop_spec` JSON to skills table.

Foundation for merging CompositeLoop into Skill (Phase 1). All existing rows keep
kind='skill' via server_default; loop_spec is NULL for all existing rows.

Revision ID: am0706_skill_kind
Revises: fc0706b_snap_uuid
"""

revision = "am0706_skill_kind"
down_revision = "fc0706b_snap_uuid"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
import sqlalchemy as sa  # noqa: E402


def upgrade() -> None:
    op.add_column(
        "skills",
        sa.Column("kind", sa.String(32), nullable=False, server_default="skill"),
    )
    op.create_index("ix_skills_kind", "skills", ["kind"])
    op.add_column(
        "skills",
        sa.Column("loop_spec", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_index("ix_skills_kind", table_name="skills")
    op.drop_column("skills", "kind")
    op.drop_column("skills", "loop_spec")
