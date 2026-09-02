"""flywheel_0902 Phase B — funnel ledger + loop runs ledger.

Revision ID: flywheel0902_funnel_ledger
Revises: founding0901_seats
Create Date: 2026-09-02 00:00:00.000000

Council v2 reconciliation (2026-09-02 §0.9) OVERRIDES the original
funnel-ledger-design.md schema in three structural ways — this migration
ships the corrected shape directly, there is no v1 to migrate from (never
deployed):

1. **TWO ledgers, not one.** ``loop_runs_ledger`` records every job
   execution (job ran / didn't fire / errored). ``funnel_events`` records
   only REAL subject-entity stage transitions. Conflating "a job ran" with
   "a person moved a stage" was Finding #1 (Confirmed) — a cron that fires
   3x/day was making ``flywheel-alive`` go green on activity that advanced
   nobody.
2. **Entity resolution, not a raw ``subject_key``/``subject_kind`` pair.**
   ``funnel_entities`` + ``funnel_identifiers`` give every stage row a
   durable ``entity_id`` so a stranger who is both an email and an IP has
   ONE identity a conversion calculation can dedupe against. Finding #3
   (Confirmed) — the v1 design's raw subject_key had no canonical identity
   and could double-count the same person across identifiers.
3. **Idempotency key = the immutable source tuple**
   ``(source_system, source_event_id, stage)``, not
   ``sha256(stage+subject+loop+date)``. Finding #2 (Confirmed) — a
   date-bucketed hash collapses same-day distinct events and can double
   count across loops/days. The source tuple is the thing that is
   ACTUALLY unique (one users.id can only ever produce one ``signup`` row,
   one Stripe invoice id can only ever produce one ``paid`` row).

Schema:
  funnel_entities
    entity_id    UUID PK
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()

  funnel_identifiers                          (alias table — no merge logic)
    id           UUID PK
    entity_id    UUID FK funnel_entities.entity_id
    kind         VARCHAR CHECK IN (email,handle,ip,api_key,user_id,stripe_customer)
    value        TEXT NOT NULL
    UNIQUE(kind, value)                        one identifier -> one entity

  funnel_events
    id                     UUID PK
    ts                     TIMESTAMPTZ NOT NULL DEFAULT now()
    stage                  VARCHAR CHECK IN (lead,contacted,replied,signup,
                            installed,bundle_created,paid)
    entity_id              UUID FK funnel_entities.entity_id
    source_system          TEXT NOT NULL         -- e.g. 'loopskill-api', 'stripe'
    source_event_id        TEXT NOT NULL         -- e.g. users.id, invoice id
    source_loop            TEXT NOT NULL         -- job_id that wrote this row
    host                   TEXT NOT NULL
    classification         VARCHAR CHECK IN (fleet,stranger,unknown)
    classification_evidence TEXT
    amount_cents           INTEGER NULL
    currency               TEXT NULL
    evidence_url           TEXT NULL
    UNIQUE(source_system, source_event_id, stage)

  loop_runs_ledger
    id           UUID PK
    ts           TIMESTAMPTZ NOT NULL DEFAULT now()
    job_id       TEXT NOT NULL
    loop_name    TEXT NOT NULL
    host         TEXT NOT NULL
    outcome      VARCHAR CHECK IN (ok,no_fire,error)
    rows_emitted INTEGER NOT NULL DEFAULT 0
    note         TEXT NULL

Indexes: funnel_events(stage, ts), funnel_events(entity_id).

Entity resolution is DELIBERATELY simple for this phase (documented in
app/services/funnel_ledger.py): ``funnel_identifiers`` is a pure alias
table, one row per (kind, value) pointing at exactly one entity. When a
LATER identifier is seen for a subject that already has a DIFFERENT
entity via another identifier, this phase does NOT merge the two
entities into one — it links the new identifier to the entity it's
first seen under and documents the split as a known limitation (M-9 in
the design doc: the full subject_key resolver is deferred). This keeps
Phase B additive and reversible; entity-merge is out of scope here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "flywheel0902_funnel_ledger"
down_revision = "founding0901_seats"
branch_labels = None
depends_on = None

_IDENTIFIER_KINDS = ("email", "handle", "ip", "api_key", "user_id", "stripe_customer")
_STAGES = (
    "lead",
    "contacted",
    "replied",
    "signup",
    "installed",
    "bundle_created",
    "paid",
)
_CLASSIFICATIONS = ("fleet", "stranger", "unknown")
_RUN_OUTCOMES = ("ok", "no_fire", "error")


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)

    op.create_table(
        "funnel_entities",
        sa.Column(
            "entity_id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "funnel_identifiers",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column("entity_id", uuid_type, nullable=False),
        sa.Column(
            "kind",
            sa.String(length=32),
            nullable=False,
        ),
        sa.Column("value", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["entity_id"], ["funnel_entities.entity_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("kind", "value", name="uq_funnel_identifiers_kind_value"),
        sa.CheckConstraint(
            "kind IN (" + ",".join(f"'{k}'" for k in _IDENTIFIER_KINDS) + ")",
            name="ck_funnel_identifiers_kind",
        ),
    )
    op.create_index("ix_funnel_identifiers_entity_id", "funnel_identifiers", ["entity_id"])

    op.create_table(
        "funnel_events",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("entity_id", uuid_type, nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("source_loop", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("classification_evidence", sa.Text(), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["entity_id"], ["funnel_entities.entity_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "source_system",
            "source_event_id",
            "stage",
            name="uq_funnel_events_source_stage",
        ),
        sa.CheckConstraint(
            "stage IN (" + ",".join(f"'{s}'" for s in _STAGES) + ")",
            name="ck_funnel_events_stage",
        ),
        sa.CheckConstraint(
            "classification IN (" + ",".join(f"'{c}'" for c in _CLASSIFICATIONS) + ")",
            name="ck_funnel_events_classification",
        ),
    )
    op.create_index("idx_funnel_events_stage_ts", "funnel_events", ["stage", "ts"])
    op.create_index("idx_funnel_events_entity_id", "funnel_events", ["entity_id"])

    op.create_table(
        "loop_runs_ledger",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("loop_name", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("rows_emitted", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "outcome IN (" + ",".join(f"'{o}'" for o in _RUN_OUTCOMES) + ")",
            name="ck_loop_runs_ledger_outcome",
        ),
    )
    op.create_index("idx_loop_runs_ledger_job_ts", "loop_runs_ledger", ["job_id", "ts"])
    op.create_index("idx_loop_runs_ledger_loop_name_ts", "loop_runs_ledger", ["loop_name", "ts"])


def downgrade() -> None:
    op.drop_index("idx_loop_runs_ledger_loop_name_ts", table_name="loop_runs_ledger")
    op.drop_index("idx_loop_runs_ledger_job_ts", table_name="loop_runs_ledger")
    op.drop_table("loop_runs_ledger")

    op.drop_index("idx_funnel_events_entity_id", table_name="funnel_events")
    op.drop_index("idx_funnel_events_stage_ts", table_name="funnel_events")
    op.drop_table("funnel_events")

    op.drop_index("ix_funnel_identifiers_entity_id", table_name="funnel_identifiers")
    op.drop_table("funnel_identifiers")

    op.drop_table("funnel_entities")
