"""add_incident_reports_and_patch_candidates

Revision ID: f1a2b3c4d5e6
Revises: e8f2a4d10b73
Create Date: 2026-05-01 10:00:00.000000

Phase B.3 — auto-improve incident network.

Creates two tables:
  incident_reports   — anonymous failure reports from `recipes-auto-improve`
  patch_candidates   — clustered (skill_id, error_signature) tuples that
                       cross the threshold of 3 distinct agents/24h

ADDITIVE ONLY.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = "f1a2b3c4d5e6"
down_revision = "e8f2a4d10b73"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    fp_type = JSONB() if dialect == "postgresql" else sa.JSON()
    id_type = UUID(as_uuid=True) if dialect == "postgresql" else sa.String(36)

    op.create_table(
        "incident_reports",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("skill_id", id_type, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("error_signature", sa.Text(), nullable=False),
        sa.Column("env_fingerprint", fp_type, nullable=False),
        sa.Column("agent_fp_anon", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stack_trace_top", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_incident_signature",
        "incident_reports",
        ["error_signature"],
    )
    op.create_index(
        "idx_incident_skill_recent",
        "incident_reports",
        ["skill_id", sa.text("occurred_at DESC")],
    )
    op.create_index(
        "idx_incident_agent",
        "incident_reports",
        ["agent_fp_anon"],
    )

    op.create_table(
        "patch_candidates",
        sa.Column("id", id_type, primary_key=True),
        sa.Column("skill_id", id_type, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("error_signature", sa.Text(), nullable=False),
        sa.Column("cluster_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_agents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_clustered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposal_path", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "skill_id", "error_signature", name="uq_patch_candidate_sig"
        ),
        sa.CheckConstraint(
            "status IN ('pending','drafted','canary','rolled_out','rolled_back','rejected')",
            name="ck_patch_candidate_status",
        ),
    )
    op.create_index(
        "idx_patch_candidate_status",
        "patch_candidates",
        ["status"],
    )
    op.create_index(
        "idx_patch_candidate_skill_sig",
        "patch_candidates",
        ["skill_id", "error_signature"],
    )


def downgrade() -> None:
    op.drop_index("idx_patch_candidate_skill_sig", table_name="patch_candidates")
    op.drop_index("idx_patch_candidate_status", table_name="patch_candidates")
    op.drop_table("patch_candidates")
    op.drop_index("idx_incident_agent", table_name="incident_reports")
    op.drop_index("idx_incident_skill_recent", table_name="incident_reports")
    op.drop_index("idx_incident_signature", table_name="incident_reports")
    op.drop_table("incident_reports")
