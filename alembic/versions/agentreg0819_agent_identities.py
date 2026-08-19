"""agentreg_0819 — agent_identities + nonces + quota, and users.is_agent.

Revision ID: agentreg0819_agent_identities
Revises: bundles0811_p1_slug_backfill
Create Date: 2026-08-19 00:00:00.000000

Agent self-registration (``POST /api/agents/register``). An autonomous agent
proves possession of an Ed25519 private key and is issued a scoped, free-tier
``rec_agent_`` API key — no human OAuth login anywhere in the path.

EDITED IN PLACE for adversarial-review round 2 (findings F1/F2/F3/F5). This
revision has never run against production — it ships for the first time in the
same PR — so the round-2 schema goes INTO it rather than into a follow-up
migration that would only exist to patch a peer that no deployed database has
ever seen.

Schema:
  agent_identities
    id               UUID PK
    pubkey           VARCHAR(128) NOT NULL UNIQUE   base64 raw ed25519 pubkey
                     (DISPLAY form)
    pubkey_sha256    VARCHAR(64)  NOT NULL UNIQUE   sha256 of the 32 RAW bytes
                     — F3: base64 is not injective onto its own text (several
                     pad-bit spellings decode to one key), so the TEXT column
                     alone could not enforce one-identity-per-key
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

  agent_registration_quota                       (F1 — the atomic enrolment cap)
    id            UUID PK
    bucket        VARCHAR(128) NOT NULL          "global" | "ip:<addr>"
    window_start  TIMESTAMPTZ NOT NULL           UTC-day floor
    count         INTEGER NOT NULL DEFAULT 0
    UNIQUE (bucket, window_start)                the row IS the lock

  users.is_agent  BOOLEAN NOT NULL DEFAULT false (F5 — the durable marker that
                  a principal is a self-registered agent and not a person)

The UNIQUE on ``nonce_hash`` is the replay wall itself: two concurrent replays
of one signed payload race at the database, and exactly one wins. The UNIQUE on
``(bucket, window_start)`` plays the same role for the enrolment cap: it
guarantees a single counter row to contend on, so the reservation can be one
conditional UPDATE instead of a count-then-compare.

DOWNGRADE IS NOT A PLAIN DROP — see :func:`downgrade`. Dropping these tables
alone would leave the shadow users and their LIVE ``rec_agent_`` keys behind,
with the revocation gate that governs them gone.
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
        sa.Column("pubkey_sha256", sa.String(length=64), nullable=False),
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
        # F3: the REAL uniqueness basis. Keyed on the decoded bytes, so no
        # alternate base64 spelling of an enrolled key can mint a second
        # identity even if the service-layer canonicality check is ever removed.
        sa.UniqueConstraint("pubkey_sha256", name="uq_agent_identities_pubkey_sha256"),
        sa.UniqueConstraint("user_id", name="uq_agent_identities_user_id"),
    )
    op.create_index("ix_agent_identities_pubkey", "agent_identities", ["pubkey"])
    op.create_index("ix_agent_identities_pubkey_sha256", "agent_identities", ["pubkey_sha256"])
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

    # Rounds 2-4 — the enrolment serialisation gate. One row per scope
    # ("global" / "ip:<address>"); locked FOR UPDATE so concurrent reservers
    # of a scope serialise, after which the service counts real
    # agent_identities rows in the exact trailing 24h. The counter-row design
    # this replaces could not express a true rolling window without a
    # boundary race or an alternate-boundary bypass (final review, N2).
    op.create_table(
        "agent_registration_gate",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_reserved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("scope", name="uq_agent_reg_gate_scope"),
    )
    op.create_index("ix_agent_reg_gate_scope", "agent_registration_gate", ["scope"])

    # F5 — the durable agent marker on the shadow user. NOT NULL with a false
    # server_default so every existing human row is backfilled to false by the
    # ALTER itself; no data migration and no nullable tri-state to reason about.
    op.add_column(
        "users",
        sa.Column("is_agent", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    """Roll back the feature AND the credentials it minted, in that order.

    F2 — why this is not a plain ``drop_table`` pair. A ``rec_agent_`` key is an
    ordinary ``api_keys`` row hanging off an ordinary (shadow) ``users`` row.
    The ONLY things that make it an agent key rather than a normal user key are
    (a) the ``agent_identities`` join the middleware consults on every request
    and (b) the ``users.is_agent`` column. Dropping those and leaving the keys
    behind does not disable them — it PROMOTES them: every live agent key keeps
    validating, now as an indistinguishable free human key, with the revocation
    gate that governed it deleted and no ``agent_identities`` row left to point
    an admin at. A rollback would have quietly converted every enrolled agent
    into an unrevocable user account.

    So the credentials go first, while the table that identifies them still
    exists to derive the set from. Order matters and is enforced by the
    dependency chain: read the user_ids FROM ``agent_identities``, delete the
    keys, delete the shadow users, and only then drop the tables.

    LOUD FAILURE IS INTENTIONAL. If an enrolled agent published a bundle or
    filed feedback, the shadow user is referenced by those rows and the DELETE
    raises a foreign-key violation, aborting the downgrade. That is the correct
    outcome: who owns an agent's published content after the feature is removed
    is a product decision, not something a migration may make silently. The
    api_keys DELETE has already neutralised the security half by then, and it
    is re-runnable.
    """
    bind = op.get_bind()

    # Derive the shadow-user set FROM agent_identities while it still exists —
    # after the drop there is no way to tell a shadow user from a human one.
    shadow_user_ids = [row[0] for row in bind.execute(sa.text("SELECT user_id FROM agent_identities"))]

    if shadow_user_ids:
        # Review round 3 (N1): the api_keys DELETE runs in its OWN committed
        # transaction, BEFORE the fallible shadow-user DELETE below. On
        # Postgres the whole migration otherwise runs in ONE transaction, so
        # a later FK failure on the users DELETE would roll the key deletion
        # back with it — the exact "safe loud failure" the docstring below
        # promised and the transaction model silently withdrew. Committing
        # the neutralization first makes that promise transactionally true:
        # if everything after this block fails, the credentials are already
        # gone and the migration is re-runnable.
        with op.get_context().autocommit_block():
            # Hard DELETE, not is_active=false: a deactivated row can be
            # flipped back on by any existing admin/support path, and after
            # this migration nothing would recognise it as an agent key.
            bind.execute(
                sa.text("DELETE FROM api_keys WHERE user_id IN (SELECT user_id FROM agent_identities)")
            )
        bind.execute(sa.text("DELETE FROM users WHERE id IN (SELECT user_id FROM agent_identities)"))

    op.drop_column("users", "is_agent")

    op.drop_index("ix_agent_reg_gate_scope", table_name="agent_registration_gate")
    op.drop_table("agent_registration_gate")
    op.drop_index("ix_agent_reg_nonce_expires", table_name="agent_registration_nonces")
    op.drop_index("ix_agent_reg_nonce_hash", table_name="agent_registration_nonces")
    op.drop_table("agent_registration_nonces")
    op.drop_index("idx_agent_identities_created", table_name="agent_identities")
    op.drop_index("idx_agent_identities_ip_created", table_name="agent_identities")
    op.drop_index("ix_agent_identities_user_id", table_name="agent_identities")
    op.drop_index("ix_agent_identities_pubkey_sha256", table_name="agent_identities")
    op.drop_index("ix_agent_identities_pubkey", table_name="agent_identities")
    op.drop_table("agent_identities")
