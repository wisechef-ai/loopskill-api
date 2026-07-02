"""loopskill_activate_0701 Phase B — Connector + ConnectorVersion + BundleConnector.

Revision ID: act0701_b_connectors
Revises: act0701_pt_syncrep
Create Date: 2026-07-02 00:00:00.000000

A Connector is a named MCP-server config fragment (stdio/http/sse) published as
a versioned artifact and deployable to fleet members via reconcile. The server
stores the TEMPLATE with ``${VAR}`` env refs only; literal secrets never
transit the server (§0.5 secret discipline). ``BundleConnector`` is the
bundle→connector provenance row, mirroring ``BundleSkill``.

NOTE (parallel-branch collision): two sibling branches (phase1, phaseA1) are
in flight and may also descend from ``lsk0627_loop_feedback``. This migration
intentionally sets ``down_revision='lsk0627_loop_feedback'`` so it is
self-contained; the parent session will run ``alembic merge`` to reconcile
heads at integration time if a multi-head collision materialises.

DOWNGRADE: drop bundle_connectors, connector_versions, connectors.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

# revision identifiers, used by Alembic.
revision = "act0701_b_connectors"
down_revision = "act0701_pt_syncrep"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create connectors + connector_versions + bundle_connectors."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    uuid_type = PG_UUID(as_uuid=True) if is_postgres else sa.Text()
    # SQLite has no native JSON; SQLAlchemy falls back to TEXT at the ORM layer.
    json_type = sa.JSON()

    # ── connectors ──
    op.create_table(
        "connectors",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("connector_type", sa.String(32), nullable=False),
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true") if is_postgres else "1",
        ),
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false") if is_postgres else "0",
        ),
        sa.Column("creator_id", uuid_type, nullable=True),
        sa.Column("org_id", uuid_type, nullable=True),
        sa.Column("residency_tag", sa.String(32), nullable=True),
        sa.Column("install_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("slug", name="uq_connectors_slug"),
    )
    op.create_index("ix_connectors_slug", "connectors", ["slug"], unique=True)

    # ── connector_versions ──
    op.create_table(
        "connector_versions",
        sa.Column(
            "id",
            uuid_type,
            primary_key=True,
            server_default=sa.text("gen_random_uuid()") if is_postgres else None,
        ),
        sa.Column(
            "connector_id",
            uuid_type,
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("semver", sa.String(32), nullable=False),
        sa.Column("config_template", json_type, nullable=False),
        sa.Column("required_env", json_type, nullable=False),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("connector_id", "semver", name="uq_connector_version"),
    )
    op.create_index("ix_connector_versions_connector_id", "connector_versions", ["connector_id"])

    # ── bundle_connectors ──
    op.create_table(
        "bundle_connectors",
        sa.Column(
            "bundle_id",
            uuid_type,
            sa.ForeignKey("bundles.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "connector_id",
            uuid_type,
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("pinned_semver", sa.String(32), nullable=True),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    """Drop bundle_connectors, connector_versions, connectors."""
    op.drop_table("bundle_connectors")
    op.drop_index("ix_connector_versions_connector_id", table_name="connector_versions")
    op.drop_table("connector_versions")
    op.drop_index("ix_connectors_slug", table_name="connectors")
    op.drop_table("connectors")
