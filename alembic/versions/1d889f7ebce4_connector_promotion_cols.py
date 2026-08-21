"""conn_promote_0821 — quality-gated staged->listed connector promotion columns.

Revision ID: 1d889f7ebce4
Revises: bd8afe172c89
Create Date: 2026-08-21 10:00:00.000000

Adds:
  * ``connectors.trust_label`` (nullable str) — "community-indexed" for any
    row minted by the automated promotion path; NULL for human-published
    connectors (unaffected). Never "curated" — that label is reserved for a
    future human editorial review this phase does not implement.
  * ``connectors.in_metasearch`` (bool, default False) — a promoted connector
    never joins the first-class metasearch fan-out without an explicit later
    decision (mirrors github_taps.GitHubTap.in_metasearch's opt-in shape).
  * ``external_connectors.promotion_status`` / ``promotion_reason`` /
    ``promoted_at`` / ``promoted_connector_id`` — promotion outcome
    bookkeeping on the staging row itself, so a failed row's reason is
    queryable without a separate audit table.

DOWNGRADE: drop all five columns. Lossy for promotion_status/reason history
(same precedent as prior backfill migrations in this repo — reconstructing
"why was this rejected" from nothing is meaningless, so downgrade does not
attempt it).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision = "1d889f7ebce4"
down_revision = "bd8afe172c89"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    uuid_type = PG_UUID(as_uuid=True) if is_postgres else sa.Text()

    op.add_column("connectors", sa.Column("trust_label", sa.String(32), nullable=True))
    op.add_column(
        "connectors",
        sa.Column(
            "in_metasearch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false") if is_postgres else "0",
        ),
    )

    op.add_column("external_connectors", sa.Column("promotion_status", sa.String(16), nullable=True))
    op.add_column("external_connectors", sa.Column("promotion_reason", sa.Text(), nullable=True))
    op.add_column("external_connectors", sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "external_connectors",
        sa.Column(
            "promoted_connector_id",
            uuid_type,
            sa.ForeignKey("connectors.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("external_connectors", "promoted_connector_id")
    op.drop_column("external_connectors", "promoted_at")
    op.drop_column("external_connectors", "promotion_reason")
    op.drop_column("external_connectors", "promotion_status")
    op.drop_column("connectors", "in_metasearch")
    op.drop_column("connectors", "trust_label")
