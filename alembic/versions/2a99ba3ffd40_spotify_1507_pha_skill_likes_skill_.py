"""spotify_1507_phA: skill_likes + skill_favourites + bundle spotify cols

Revision ID: 2a99ba3ffd40
Revises: liked0711_p2
Create Date: 2026-07-15 16:30:31.634504

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a99ba3ffd40'
down_revision: Union[str, Sequence[str], None] = 'liked0711_p2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── skill_likes table ──────────────────────────────────────────────────
    op.create_table(
        "skill_likes",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("skill_id", sa.UUID(as_uuid=True), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("federated_source", sa.String(64), nullable=True, index=True),
        sa.Column("federated_slug", sa.String(255), nullable=True, index=True),
        sa.Column("liked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "skill_id", name="uq_skill_likes_user_local"),
        sa.UniqueConstraint("user_id", "federated_source", "federated_slug", name="uq_skill_likes_user_federated"),
    )

    # ── skill_favourites table ─────────────────────────────────────────────
    op.create_table(
        "skill_favourites",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("skill_id", sa.UUID(as_uuid=True), sa.ForeignKey("skills.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("federated_source", sa.String(64), nullable=True, index=True),
        sa.Column("federated_slug", sa.String(255), nullable=True, index=True),
        sa.Column("favourited_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "skill_id", name="uq_skill_favourites_user_local"),
        sa.UniqueConstraint("user_id", "federated_source", "federated_slug", name="uq_skill_favourites_user_federated"),
    )

    # ── Bundle Spotify columns ─────────────────────────────────────────────
    op.add_column("bundles", sa.Column("follower_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("bundles", sa.Column("is_editorial", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("bundles", sa.Column("curated_by", sa.String(32), nullable=True))


def downgrade() -> None:
    op.drop_column("bundles", "curated_by")
    op.drop_column("bundles", "is_editorial")
    op.drop_column("bundles", "follower_count")
    op.drop_table("skill_favourites")
    op.drop_table("skill_likes")
