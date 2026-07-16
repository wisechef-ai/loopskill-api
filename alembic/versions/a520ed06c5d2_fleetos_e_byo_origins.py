"""fleetos_1607 Phase E — artifact_origins + origin_drift_events

Revision ID: a520ed06c5d2
Revises: ca2afa8c1bf5
Create Date: 2026-07-16

Phase E of fleetos_1607 — BYO-repo registries (metadata-only = the hyperscale
gate). Two additive tables:

  * artifact_origins    — SHA-pinned origin (github:owner/repo@sha:path) +
                          content-hash LOCK. The server stores this metadata;
                          agents fetch the bytes directly from the user's repo
                          and verify against the lock. Server stores no content.
  * origin_drift_events — audit trail of hash-verification failures (force-push,
                          tampering, wrong SHA served) — the honest failure
                          surface of the BYO-repo trade-off.

Portable SQL only (plain CREATE TABLE / UNIQUE / INDEX). Applies identically on
Postgres and SQLite. Additive-only, no data migration.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a520ed06c5d2"
down_revision: Union[str, Sequence[str], None] = "ca2afa8c1bf5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _uuid() -> sa.types.TypeEngine:
    return postgresql.UUID(as_uuid=True).with_variant(sa.CHAR(32), "sqlite")


def upgrade() -> None:
    """Upgrade schema — two additive tables."""
    op.create_table(
        "artifact_origins",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("owner_user_id", _uuid(), nullable=True),
        sa.Column("org_id", _uuid(), nullable=True),
        sa.Column("artifact_kind", sa.String(length=32), nullable=False),
        sa.Column("artifact_key", sa.String(length=255), nullable=False),
        sa.Column("repo", sa.String(length=512), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("fetch_secret_ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "owner_user_id",
            "org_id",
            "artifact_kind",
            "artifact_key",
            name="uq_artifact_origin_scope_key",
        ),
    )
    op.create_index("idx_artifact_origin_repo", "artifact_origins", ["repo", "commit_sha"])
    op.create_index(op.f("ix_artifact_origins_owner_user_id"), "artifact_origins", ["owner_user_id"])
    op.create_index(op.f("ix_artifact_origins_org_id"), "artifact_origins", ["org_id"])

    op.create_table(
        "origin_drift_events",
        sa.Column("id", _uuid(), primary_key=True, nullable=False),
        sa.Column("origin_id", _uuid(), nullable=True),
        sa.Column("member_id", _uuid(), nullable=True),
        sa.Column("repo", sa.String(length=512), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("expected_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_hash", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["origin_id"], ["artifact_origins.id"], ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_origin_drift_events_origin_id"), "origin_drift_events", ["origin_id"])
    op.create_index(op.f("ix_origin_drift_events_member_id"), "origin_drift_events", ["member_id"])


def downgrade() -> None:
    """Downgrade schema — drop the two tables."""
    op.drop_index(op.f("ix_origin_drift_events_member_id"), table_name="origin_drift_events")
    op.drop_index(op.f("ix_origin_drift_events_origin_id"), table_name="origin_drift_events")
    op.drop_table("origin_drift_events")

    op.drop_index(op.f("ix_artifact_origins_org_id"), table_name="artifact_origins")
    op.drop_index(op.f("ix_artifact_origins_owner_user_id"), table_name="artifact_origins")
    op.drop_index("idx_artifact_origin_repo", table_name="artifact_origins")
    op.drop_table("artifact_origins")
