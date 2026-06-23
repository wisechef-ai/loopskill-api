"""spotify_0608/B — api_keys.is_test for install-count integrity

Revision ID: spotify_0608_b_apikey_is_test
Revises: spotify_0608_a_cb_absorbs_bkt
Create Date: 2026-06-09 06:00:00.000000

spotify_0608 Phase B (§4.2). Adds a nullable-default `is_test` boolean to
`api_keys` so synthetic install traffic (test / CI / internal harness keys) can
be EXCLUDED from every public-ranking surface — `_install_counts_for`, the
carousel popularity term, the discover ranking, the leaderboards, and the GTM
kill/scale install signal. Default false = organic.

DOWNGRADE: drop the column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "spotify_0608_b_apikey_is_test"
down_revision = "spotify_0608_a_cb_absorbs_bkt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("is_test", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("api_keys", "is_test")
