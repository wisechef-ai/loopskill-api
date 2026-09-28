"""paywall_0925 — paywall_hits + user_sync_days (instrument the paywall).

Revision ID: paywall0925_hits
Revises: ah0916_msq_client_ip
Create Date: 2026-09-25 13:00:00.000000

WHY THIS EXISTS (an instrument gap, not a feature):

Every live tier gate (private-bundle cap, deploy tier, fleet-member cap, forks,
API-key cap, MCP compose quota) answered 402/403 and recorded nothing, and the
one paywall metric the admin pulse reported (``users.free_sync_used_at``) had
no writer anywhere in app code — "paywall pressure" was 0 by construction. So
"has a stranger ever reached a paywall?" was unanswerable.

* ``paywall_hits`` — one row per (gate, subject, UTC day) a gate refused, with
  a ``hit_count`` for repeats and a fleet/stranger/unknown classification
  computed once at write time (funnel_ledger.classify).
* ``user_sync_days`` — one row per (user, UTC day) with a sync; feeds
  ``repeat_sync_users_30d``.

Why a dedicated table and not a ``paywall_hit`` stage on ``funnel_events``:
funnel_events is idempotent per ``(source_system, source_event_id, stage)`` and
means "a subject moved a stage, once". A paywall is hit repeatedly by the same
subject and we want the gate name + repeat count; forcing that into
funnel_events would need a synthetic event id per hit and a CHECK-constraint
rewrite (drop + recreate on Postgres). A new table is purely additive.

ADDITIVE ONLY: creates two tables + indexes; touches no existing table.
Downgrade drops exactly what upgrade created. ``users.free_sync_used_at`` is
NOT dropped here (additive-only rule) even though nothing reads it any more —
see the PR for that decision.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "paywall0925_hits"
down_revision = "ah0916_msq_client_ip"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_type = postgresql.UUID(as_uuid=True) if is_pg else sa.String(36)

    op.create_table(
        "paywall_hits",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()") if is_pg else None,
        ),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("gate", sa.String(length=64), nullable=False),
        sa.Column("subject_key", sa.String(length=128), nullable=False),
        sa.Column("user_id", uuid_type, nullable=True),
        sa.Column("api_key_id", uuid_type, nullable=True),
        sa.Column("tier", sa.String(length=32), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column("classification", sa.String(length=16), nullable=False),
        sa.Column("classification_evidence", sa.Text(), nullable=True),
        sa.Column("hit_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("first_hit_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("gate", "subject_key", "day", name="uq_paywall_hits_gate_subject_day"),
        sa.CheckConstraint(
            "classification IN ('fleet','stranger','unknown')",
            name="ck_paywall_hits_classification",
        ),
    )
    op.create_index("idx_paywall_hits_day_gate", "paywall_hits", ["day", "gate"])
    op.create_index("ix_paywall_hits_user_id", "paywall_hits", ["user_id"])

    op.create_table(
        "user_sync_days",
        sa.Column("user_id", uuid_type, primary_key=True, nullable=False),
        sa.Column("day", sa.Date(), primary_key=True, nullable=False),
        sa.Column("first_source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_user_sync_days_day", "user_sync_days", ["day"])


def downgrade() -> None:
    op.drop_index("idx_user_sync_days_day", table_name="user_sync_days")
    op.drop_table("user_sync_days")
    op.drop_index("ix_paywall_hits_user_id", table_name="paywall_hits")
    op.drop_index("idx_paywall_hits_day_gate", table_name="paywall_hits")
    op.drop_table("paywall_hits")
