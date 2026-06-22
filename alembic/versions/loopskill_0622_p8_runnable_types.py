"""loopskill_0622 Phase 8 — runnable catalog types: loops + personalities

Revision ID: loopskill_0622_p8_runnable_types
Revises: portal_0610_j2_install_order
Create Date: 2026-06-22 17:00:00.000000

LoopSkill's star engine. Adds the two RUNNABLE catalog types (loops,
personalities) plus their version tables. New, clean-vocab schema — no
cookbook/recipe lineage — so it ships in v1 independent of the P3/P4 rename.

A `loop` carries a safety-bounded execution contract as structured columns
(success_condition, verification_script, max_turns, budget_usd, stopping_criteria,
tool_allowlist, system_prompt) so the registry validates it on publish and a
runner can enforce it. A `personality` carries a system_prompt + JSON config.

DOWNGRADE: drop all four tables (version tables first for FK order).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "loopskill_0622_p8_runnable_types"
down_revision = "portal_0610_j2_install_order"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── loops ──────────────────────────────────────────────────────────────
    op.create_table(
        "loops",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("readme", sa.Text(), nullable=True),
        sa.Column("license", sa.String(64), nullable=True),
        sa.Column("tier", sa.String(32), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("creator_id", UUID(as_uuid=True), sa.ForeignKey("creators.id"), nullable=True),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("orgs.id"), nullable=True),
        # safety-bounded contract
        sa.Column("success_condition", sa.Text(), nullable=False),
        sa.Column("verification_script", sa.Text(), nullable=False),
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default=sa.text("25")),
        sa.Column("budget_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("stopping_criteria", sa.JSON(), nullable=False),
        sa.Column("tool_allowlist", sa.JSON(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("install_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rating_avg", sa.Float(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_loops_slug", "loops", ["slug"], unique=True)
    op.create_index("ix_loops_category", "loops", ["category"])

    op.create_table(
        "loop_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("loop_id", UUID(as_uuid=True), sa.ForeignKey("loops.id"), nullable=False),
        sa.Column("semver", sa.String(32), nullable=False),
        sa.Column("tarball_path", sa.Text(), nullable=True),
        sa.Column("tarball_size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("manifest", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("loop_id", "semver", name="uq_loop_version"),
    )
    op.create_index("ix_loop_versions_loop_id", "loop_versions", ["loop_id"])

    # ── personalities ──────────────────────────────────────────────────────
    op.create_table(
        "personalities",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(128), nullable=True),
        sa.Column("readme", sa.Text(), nullable=True),
        sa.Column("license", sa.String(64), nullable=True),
        sa.Column("tier", sa.String(32), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("creator_id", UUID(as_uuid=True), sa.ForeignKey("creators.id"), nullable=True),
        sa.Column("org_id", UUID(as_uuid=True), sa.ForeignKey("orgs.id"), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("install_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("rating_avg", sa.Float(), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_personalities_slug", "personalities", ["slug"], unique=True)
    op.create_index("ix_personalities_category", "personalities", ["category"])

    op.create_table(
        "personality_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "personality_id",
            UUID(as_uuid=True),
            sa.ForeignKey("personalities.id"),
            nullable=False,
        ),
        sa.Column("semver", sa.String(32), nullable=False),
        sa.Column("tarball_path", sa.Text(), nullable=True),
        sa.Column("tarball_size_bytes", sa.Integer(), nullable=True),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("manifest", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("personality_id", "semver", name="uq_personality_version"),
    )
    op.create_index(
        "ix_personality_versions_personality_id",
        "personality_versions",
        ["personality_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_personality_versions_personality_id", table_name="personality_versions")
    op.drop_table("personality_versions")
    op.drop_index("ix_personalities_category", table_name="personalities")
    op.drop_index("ix_personalities_slug", table_name="personalities")
    op.drop_table("personalities")
    op.drop_index("ix_loop_versions_loop_id", table_name="loop_versions")
    op.drop_table("loop_versions")
    op.drop_index("ix_loops_category", table_name="loops")
    op.drop_index("ix_loops_slug", table_name="loops")
    op.drop_table("loops")
