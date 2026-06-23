"""Add skill_publish_requests table.

Phase C (recipes_2005): guarded creator-onboarding path. Public-skill publish
requests are stored here before being dispatched for human review on GitHub.
The skill-publish-approver workflow reads tarball_bytes via the admin endpoint
when the reviewer labels the issue 'approved'.

Revision ID: c3051b7d2005
Revises: c4d5e6f7a8b9
Create Date: 2026-05-20 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "c3051b7d2005"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)

    op.create_table(
        "skill_publish_requests",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            # gen_random_uuid() is Postgres-only; on SQLite the UUID is
            # always supplied by the ORM (uuid4()), so no server default needed.
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("tarball_bytes", sa.LargeBinary(), nullable=True),
        sa.Column(
            "requester_user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "requester_creator_id",
            uuid_type,
            sa.ForeignKey("creators.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("issue_url", sa.Text(), nullable=True),
        sa.Column("issue_number", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','shipped')",
            name="ck_spr_status",
        ),
    )
    op.create_index("idx_spr_slug", "skill_publish_requests", ["slug"])
    op.create_index(
        "idx_spr_slug_created",
        "skill_publish_requests",
        ["slug", "created_at"],
    )
    op.create_index("idx_spr_status", "skill_publish_requests", ["status"])
    op.create_index(
        "idx_spr_requester_user",
        "skill_publish_requests",
        ["requester_user_id"],
    )
    op.create_index(
        "idx_spr_requester_creator",
        "skill_publish_requests",
        ["requester_creator_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_spr_requester_creator", table_name="skill_publish_requests")
    op.drop_index("idx_spr_requester_user", table_name="skill_publish_requests")
    op.drop_index("idx_spr_status", table_name="skill_publish_requests")
    op.drop_index("idx_spr_slug_created", table_name="skill_publish_requests")
    op.drop_index("idx_spr_slug", table_name="skill_publish_requests")
    op.drop_table("skill_publish_requests")
