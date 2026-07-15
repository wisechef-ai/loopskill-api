"""spotify_1507_phB: bundle_locks + skill compat + bundleskill pin_mode

Revision ID: e551aae04e88
Revises: 2a99ba3ffd40
Create Date: 2026-07-15 17:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e551aae04e88'
down_revision: Union[str, Sequence[str], None] = '2a99ba3ffd40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── bundle_locks: the immutable published-snapshot primitive ────────────
    op.create_table(
        "bundle_locks",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("bundle_id", sa.UUID(as_uuid=True), sa.ForeignKey("bundles.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("locked_entries", sa.JSON(), nullable=False),
        sa.Column("lock_hash", sa.String(64), nullable=False),
        sa.Column("created_by", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("bundle_id", "revision", name="uq_bundle_locks_bundle_revision"),
    )
    op.create_index("ix_bundle_locks_bundle_rev", "bundle_locks", ["bundle_id", "revision"])

    # ── Skill compat metadata (drift-killer compat alerts) ──────────────────
    op.add_column("skills", sa.Column("compat_targets", sa.JSON(), nullable=True))
    op.add_column("skills", sa.Column("compat_status", sa.String(32), nullable=False, server_default="active"))
    op.add_column("skills", sa.Column("compat_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_skills_compat_status", "skills", ["compat_status"])

    # ── BundleSkill entry-level pin_mode (track|pin) ─────────────────────────
    op.add_column("bundle_skills", sa.Column("pin_mode", sa.String(16), nullable=False, server_default="track"))


def downgrade() -> None:
    op.drop_column("bundle_skills", "pin_mode")
    op.drop_index("ix_skills_compat_status", table_name="skills")
    op.drop_column("skills", "compat_checked_at")
    op.drop_column("skills", "compat_status")
    op.drop_column("skills", "compat_targets")
    op.drop_index("ix_bundle_locks_bundle_rev", table_name="bundle_locks")
    op.drop_table("bundle_locks")
