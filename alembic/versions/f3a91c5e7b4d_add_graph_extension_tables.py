"""add_graph_extension_tables

Revision ID: f3a91c5e7b4d
Revises: f1a2b3c4d5e6
Create Date: 2026-05-01 09:00:00.000000

Phase B.5 (v5.4) — Skill graph extends from 3 to 6 edge types.

Adds two tables to back the new `replaced_by` edge type:
  - `skill_replacements`     — manual curator edits (master API key only)
  - `replacement_candidates` — auto-detected pending review (cron-fed)

The other three new edge types (failed_after, arch_compatible_with, and
category_sibling) are derived on read from existing tables (incident_reports,
install_events.host_fingerprint, skill_derived_edges) and need no schema
additions here.

ADDITIVE ONLY — no existing column or table touched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision = "f3a91c5e7b4d"
down_revision = "f1a9c0d3e711"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    json_type = JSONB() if dialect == "postgresql" else sa.JSON()
    id_type = UUID(as_uuid=True) if dialect == "postgresql" else sa.String(36)

    op.create_table(
        "skill_replacements",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("source_id", id_type, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("target_id", id_type, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source_id", "target_id", name="uq_skill_replacement_pair"),
    )
    op.create_index("ix_skill_replacements_source", "skill_replacements", ["source_id"])
    op.create_index("ix_skill_replacements_target", "skill_replacements", ["target_id"])

    op.create_table(
        "replacement_candidates",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("source_id", id_type, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("target_id", id_type, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("evidence_json", json_type, nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("source_id", "target_id", name="uq_replacement_candidate_pair"),
    )
    op.create_index("ix_replacement_candidates_source", "replacement_candidates", ["source_id"])
    op.create_index("ix_replacement_candidates_status", "replacement_candidates", ["status"])


def downgrade() -> None:
    op.drop_index("ix_replacement_candidates_status", table_name="replacement_candidates")
    op.drop_index("ix_replacement_candidates_source", table_name="replacement_candidates")
    op.drop_table("replacement_candidates")

    op.drop_index("ix_skill_replacements_target", table_name="skill_replacements")
    op.drop_index("ix_skill_replacements_source", table_name="skill_replacements")
    op.drop_table("skill_replacements")
