"""bundles0811_p35 — federation_registry_proposals table.

Revision ID: b0811_p35_fed_propose
Revises: mesh0408_w2_sub_event_at
Create Date: 2026-08-11 00:00:00.000000

bundles_0811 Phase P3.5 (locked decision #10): self-serve federation registry
proposals. A proposal row records a submission to POST /api/federation/propose
or loopskill_propose_registry; ACCEPTING a proposal is a config edit to
config/federation_sources.yaml, not a schema/row mutation.

Schema:
  id                   UUID PK
  repo_url             TEXT NOT NULL
  proposed_source_id   TEXT NOT NULL
  contact              TEXT NULL
  why                  TEXT NULL
  identity             TEXT NOT NULL   rate-limit identity
  signature            TEXT NOT NULL   sha256(identity|repo_url) — 24h dedupe key
  repo_exists          BOOLEAN NULL    pre-flight evidence
  skill_md_count       INT NULL        pre-flight evidence
  license_detected     TEXT NULL       pre-flight evidence
  preflight_summary    JSON NULL       full pre-flight evidence blob
  status               TEXT CHECK ('pending','accepted','rejected') DEFAULT 'pending'
  issue_url            TEXT NULL
  created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()

DOWNGRADE: DROP TABLE federation_registry_proposals
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b0811_p35_fed_propose"
down_revision = "mesh0408_w2_sub_event_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)

    op.create_table(
        "federation_registry_proposals",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("proposed_source_id", sa.Text(), nullable=False),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column("why", sa.Text(), nullable=True),
        sa.Column("identity", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("repo_exists", sa.Boolean(), nullable=True),
        sa.Column("skill_md_count", sa.Integer(), nullable=True),
        sa.Column("license_detected", sa.Text(), nullable=True),
        sa.Column("preflight_summary", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("issue_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','accepted','rejected')",
            name="ck_frp_status",
        ),
    )
    op.create_index("idx_frp_signature", "federation_registry_proposals", ["signature"])
    op.create_index(
        "idx_frp_identity_created",
        "federation_registry_proposals",
        ["identity", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_frp_identity_created", table_name="federation_registry_proposals")
    op.drop_index("idx_frp_signature", table_name="federation_registry_proposals")
    op.drop_table("federation_registry_proposals")
