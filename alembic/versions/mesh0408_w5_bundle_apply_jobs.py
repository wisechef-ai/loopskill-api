"""mesh0408_w5_bundle_apply_jobs — terminal state for the bundle deploy path

Revision ID: mesh0408_w5_applyjobs
Revises: mesh0408_t1c_extconn
Create Date: 2026-08-07

mesh_0408 W5. Creates ``bundle_apply_jobs`` + ``bundle_apply_job_items`` — the
persistence behind the bundle path's first real terminal state.

Before this, ``POST /api/bundle-deploy/{id}/apply`` synthesized a ``uuid4()``
job id and discarded it, and the status endpoint answered a hard-coded
``{"status": "applying"}`` for ANY id, forever. Nothing could ever go red.

``bundle_apply_job_items.expected_semver`` is the load-bearing column: it pins
what the bundle resolved to when the job opened, so ``converged`` requires the
member to report success AT that version. A member still running the defective
version cannot green the job by asserting success.

Both engines (mesh0408 T0-A CI runs Postgres and SQLite): plain create_table
with a dialect-appropriate UUID type, no ALTER path, so no dialect branch is
needed beyond the type/default selection already used by
``mesh0408_t1c_external_connectors``.

DOWNGRADE: DROP both tables (items first — FK dependency).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision: str = "mesh0408_w5_applyjobs"
down_revision: Union[str, Sequence[str], None] = "mesh0408_t1c_extconn"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    uuid_type = PG_UUID(as_uuid=True) if is_postgres else sa.String(36)
    uuid_default = sa.text("gen_random_uuid()") if is_postgres else None

    op.create_table(
        "bundle_apply_jobs",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column(
            "bundle_id",
            uuid_type,
            sa.ForeignKey("bundles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NULL = operator-initiated (portal apply); set = agent-initiated.
        sa.Column(
            "member_id",
            uuid_type,
            sa.ForeignKey("fleet_members.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requested_by_user_id", uuid_type, nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="applying",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Stamped once, on the first transition into converged|failed.
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_bundle_apply_jobs_bundle_id", "bundle_apply_jobs", ["bundle_id"])
    op.create_index("ix_bundle_apply_jobs_member_id", "bundle_apply_jobs", ["member_id"])

    op.create_table(
        "bundle_apply_job_items",
        sa.Column("id", uuid_type, primary_key=True, server_default=uuid_default),
        sa.Column(
            "job_id",
            uuid_type,
            sa.ForeignKey("bundle_apply_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("skill_id", uuid_type, sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("skill_slug", sa.String(length=255), nullable=False),
        # What the bundle resolved to when the job opened. Convergence compares
        # the member's reported semver against THIS.
        sa.Column("expected_semver", sa.String(length=64), nullable=False),
        # NULL until the member reports.
        sa.Column("outcome", sa.String(length=20), nullable=True),
        sa.Column("reported_semver", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("job_id", "skill_id", name="uq_bundle_apply_job_items_job_skill"),
    )
    op.create_index("ix_bundle_apply_job_items_job_id", "bundle_apply_job_items", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_bundle_apply_job_items_job_id", table_name="bundle_apply_job_items")
    op.drop_table("bundle_apply_job_items")
    op.drop_index("ix_bundle_apply_jobs_member_id", table_name="bundle_apply_jobs")
    op.drop_index("ix_bundle_apply_jobs_bundle_id", table_name="bundle_apply_jobs")
    op.drop_table("bundle_apply_jobs")
