"""activate_0701 Phase A2 — composite_loops, composite_loop_versions,
bundle_composite_loops, bundle_personalities.

Revision ID: act0701_a2_cl
Revises: act0701_pt_syncrep
Create Date: 2026-07-02 18:00:00.000000

Creates the four Phase A2 tables for the COMPOSITE LOOP + PERSONALITY DEPLOY
contract (docs/design/activate0701-phaseA2-composite-loop.md).

Tables:
    composite_loops          the composite loop registry (NEW surface, council §6)
    composite_loop_versions  frozen versioned manifests
    bundle_composite_loops   join: bundle declares composite loop
    bundle_personalities     join: bundle declares personality (verified-absent path)
"""

# revision identifiers used by Alembic.
revision = "act0701_a2_cl"
down_revision = "act0701_b_connectors"
branch_labels = None
depends_on = None

from alembic import op  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402


def upgrade() -> None:
    op.create_table(
        "composite_loops",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(255), unique=True, nullable=False, index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("creator_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("creators.id"), nullable=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orgs.id"), nullable=True),
        sa.Column("tier", sa.String(32), nullable=True),
        sa.Column("schedule", sa.Text(), nullable=False),
        sa.Column(
            "skills", postgresql.JSON(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")
        ),
        sa.Column(
            "connectors",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "subagents_config",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("verifier_slug", sa.String(255), nullable=False),
        sa.Column(
            "state_seed",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("budget_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("residency", sa.String(32), nullable=True),
        sa.Column("install_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        "composite_loop_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "composite_loop_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("composite_loops.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("semver", sa.String(32), nullable=False),
        sa.Column("manifest", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("composite_loop_id", "semver", name="uq_composite_loop_version"),
    )

    op.create_table(
        "bundle_composite_loops",
        sa.Column(
            "bundle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bundles.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "composite_loop_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("composite_loops.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("pinned_version", sa.String(50), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bundle_cl_bundle", "bundle_composite_loops", ["bundle_id"])

    op.create_table(
        "bundle_personalities",
        sa.Column(
            "bundle_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bundles.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "personality_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("personalities.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("pinned_version", sa.String(50), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_bundle_pers_bundle", "bundle_personalities", ["bundle_id"])


def downgrade() -> None:
    op.drop_index("ix_bundle_pers_bundle", table_name="bundle_personalities")
    op.drop_table("bundle_personalities")
    op.drop_index("ix_bundle_cl_bundle", table_name="bundle_composite_loops")
    op.drop_table("bundle_composite_loops")
    op.drop_table("composite_loop_versions")
    op.drop_table("composite_loops")
