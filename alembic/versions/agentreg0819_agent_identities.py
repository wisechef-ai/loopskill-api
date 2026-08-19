"""agentreg_0819 — agent_identities + agent_registration_nonces.

Revision ID: agentreg0819_agent_identities
Revises: bundles0811_p1_slug_backfill
Create Date: 2026-08-19 00:00:00.000000

Agent self-registration (``POST /api/agents/register``). An autonomous agent
proves possession of an Ed25519 private key and is issued a scoped, free-tier
``rec_agent_`` API key — no human OAuth login anywhere in the path.

Schema:
  agent_identities
    id               UUID PK
    pubkey           VARCHAR(128) NOT NULL UNIQUE   base64 raw ed25519 pubkey
    agent_name       VARCHAR(64)  NOT NULL
    contact          VARCHAR(128) NULL
    user_id          UUID NOT NULL UNIQUE FK users.id ON DELETE CASCADE
                     (the SHADOW user the agent's API keys hang off — see the
                     AgentIdentity model docstring for why a shadow row beats
                     a nullable api_keys.user_id)
    revoked          BOOLEAN NOT NULL DEFAULT false
    registration_ip  VARCHAR(64) NULL
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()

  agent_registration_nonces
    id          UUID PK
    nonce_hash  VARCHAR(64) NOT NULL UNIQUE   sha256 hex of the client nonce
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    expires_at  TIMESTAMPTZ NOT NULL

The UNIQUE on ``nonce_hash`` is the replay wall itself: two concurrent replays
of one signed payload race at the database, and exactly one wins.

DOWNGRADE: DROP TABLE agent_registration_nonces, agent_identities.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "agentreg0819_agent_identities"
down_revision = "bundles0811_p1_slug_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)

    op.create_table(
        "agent_identities",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column("pubkey", sa.String(length=128), nullable=False),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("contact", sa.String(length=128), nullable=True),
        sa.Column("user_id", uuid_type, nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("registration_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("pubkey", name="uq_agent_identities_pubkey"),
        sa.UniqueConstraint("user_id", name="uq_agent_identities_user_id"),
    )
    op.create_index("ix_agent_identities_pubkey", "agent_identities", ["pubkey"])
    op.create_index("ix_agent_identities_user_id", "agent_identities", ["user_id"])
    op.create_index(
        "idx_agent_identities_ip_created",
        "agent_identities",
        ["registration_ip", "created_at"],
    )
    op.create_index("idx_agent_identities_created", "agent_identities", ["created_at"])

    op.create_table(
        "agent_registration_nonces",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("nonce_hash", name="uq_agent_reg_nonce_hash"),
    )
    op.create_index("ix_agent_reg_nonce_hash", "agent_registration_nonces", ["nonce_hash"])
    op.create_index("ix_agent_reg_nonce_expires", "agent_registration_nonces", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_reg_nonce_expires", table_name="agent_registration_nonces")
    op.drop_index("ix_agent_reg_nonce_hash", table_name="agent_registration_nonces")
    op.drop_table("agent_registration_nonces")
    op.drop_index("idx_agent_identities_created", table_name="agent_identities")
    op.drop_index("idx_agent_identities_ip_created", table_name="agent_identities")
    op.drop_index("ix_agent_identities_user_id", table_name="agent_identities")
    op.drop_index("ix_agent_identities_pubkey", table_name="agent_identities")
    op.drop_table("agent_identities")
