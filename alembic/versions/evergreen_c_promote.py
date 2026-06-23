"""evergreen_0206 Phase C — channel-aware fleet sync: version promotion state

Revision ID: evergreen_c_promote
Revises: lc3005_j_feedback_repo
Create Date: 2026-06-03 00:00:00.000000

Adds one nullable column to ``skill_versions``:

  promoted_to_stable_at  TIMESTAMPTZ  — when this version passed the health/eval
                                        gate and became eligible for the STABLE
                                        channel. NULL → canary-only (not yet
                                        promoted). Written by the Phase E
                                        promotion engine; read by channel-aware
                                        version selection (Phase C).

Channel semantics (fleet_sync + reconcile version-selection):
  canary → latest semver (any version, promoted or not)
  stable → latest semver WHERE promoted_to_stable_at IS NOT NULL
  frozen → no version movement (pinned hold)

Postgres-only SQL.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "evergreen_c_promote"
down_revision = "lc3005_j_feedback_repo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add promoted_to_stable_at to skill_versions."""
    op.add_column(
        "skill_versions",
        sa.Column("promoted_to_stable_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Index for the stable-channel selection query (latest promoted per skill).
    op.create_index(
        "ix_skill_versions_promoted",
        "skill_versions",
        ["skill_id", "promoted_to_stable_at"],
    )


def downgrade() -> None:
    """Drop promoted_to_stable_at + its index."""
    op.drop_index("ix_skill_versions_promoted", table_name="skill_versions")
    op.drop_column("skill_versions", "promoted_to_stable_at")
