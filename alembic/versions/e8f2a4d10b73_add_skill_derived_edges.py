"""add_skill_derived_edges

Revision ID: e8f2a4d10b73
Revises: c4d8e3f1a902
Create Date: 2026-04-30 18:30:00.000000

Stage 2 (G16) — Skill-graph derived edges.

Creates `skill_derived_edges` for the algorithmic edges produced by
`app.edge_builder.build_edges`. Stage-1 declared edges remain in
`skills.related_skills`; the two surfaces are unioned by
`GET /api/skills/{slug}/graph`.

ADDITIVE ONLY — no existing column or table touched. Rebuilds are
idempotent (delete-then-insert).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = "e8f2a4d10b73"
down_revision = "c4d8e3f1a902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    signals_type = JSONB() if dialect == "postgresql" else sa.JSON()
    id_type = UUID(as_uuid=True) if dialect == "postgresql" else sa.String(36)

    op.create_table(
        "skill_derived_edges",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("source_slug", sa.String(255), nullable=False),
        sa.Column("target_slug", sa.String(255), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("signals", signals_type, nullable=True),
        sa.Column(
            "last_built_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source_slug", "target_slug",
                            name="uq_skill_edge_pair"),
    )
    op.create_index(
        "ix_skill_derived_edges_source",
        "skill_derived_edges",
        ["source_slug"],
    )
    op.create_index(
        "ix_skill_derived_edges_target",
        "skill_derived_edges",
        ["target_slug"],
    )
    op.create_index(
        "ix_skill_derived_edges_weight",
        "skill_derived_edges",
        ["weight"],
    )


def downgrade() -> None:
    op.drop_index("ix_skill_derived_edges_weight",
                  table_name="skill_derived_edges")
    op.drop_index("ix_skill_derived_edges_target",
                  table_name="skill_derived_edges")
    op.drop_index("ix_skill_derived_edges_source",
                  table_name="skill_derived_edges")
    op.drop_table("skill_derived_edges")
