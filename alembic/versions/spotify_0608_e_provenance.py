"""spotify_0608/E — install provenance + cookbook_id + attribution

Revision ID: spotify_0608_e_provenance
Revises: spotify_0608_b_apikey_is_test
Create Date: 2026-06-09 12:40:00.000000

spotify_0608 Phase E (feedback-harness + provenance, boil-the-ocean).

The Sentry/npm install-provenance pattern (R2/R3/R4 data-model contract):

  1. ``install_events.cookbook_id`` (nullable) — which cookbook the install was
     triggered from. Threaded through ``_record_install_event()``.
  2. ``install_events.attribution`` — 'attributed' (default) | 'unattributed'.
     Deep-link / non-fetch installs honestly stamp 'unattributed' (no body →
     no deeper attribution); transient fetch failures are NOT mis-stamped (they
     stay hard errors, never reach here).
  3. ``provenance_records`` — a RANDOM, server-stored opaque token
     (``provenance_id = secrets.token_urlsafe(32)``) mapping → install_event_id.
     The token carries ZERO client-readable metadata (fixes the itsdangerous
     signed-but-not-encrypted leak from the first design). Resolution is a
     server-side DB lookup: provenance_id → install_event → (cookbook_id,
     skill_id, version_semver). Feedback / skill-error carries the
     provenance_id; the server resolves it and routes the issue to the correct
     creator repo — deterministic, replacing the "first cookbook the user owns"
     guess.

DOWNGRADE: drop provenance_records, then the two install_events columns.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "spotify_0608_e_provenance"
down_revision = "spotify_0608_b_apikey_is_test"
branch_labels = None
depends_on = None


def _uuid_type(is_pg: bool):
    """UUID column type for Postgres, String(36) elsewhere (SQLite)."""
    return postgresql.UUID(as_uuid=True) if is_pg else sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    uuid_t = _uuid_type(is_pg)

    op.add_column("install_events", sa.Column("cookbook_id", uuid_t, nullable=True))
    op.add_column(
        "install_events",
        sa.Column(
            "attribution",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'attributed'"),
        ),
    )
    op.create_index(
        "ix_install_events_cookbook_id", "install_events", ["cookbook_id"], unique=False
    )

    op.create_table(
        "provenance_records",
        sa.Column("provenance_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column(
            "install_event_id",
            uuid_t,
            sa.ForeignKey("install_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_provenance_records_event", "provenance_records", ["install_event_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_provenance_records_event", table_name="provenance_records")
    op.drop_table("provenance_records")
    op.drop_index("ix_install_events_cookbook_id", table_name="install_events")
    op.drop_column("install_events", "attribution")
    op.drop_column("install_events", "cookbook_id")
