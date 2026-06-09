"""spotify_0608/G — cookbooks.is_verified (verified-maintainer badge)

Revision ID: spotify_0608_g_verified
Revises: spotify_0608_e_provenance
Create Date: 2026-06-09 15:45:00.000000

spotify_0608 Phase G (reputation surfaces). Adds a nullable-default
`is_verified` boolean to `cookbooks` — the verified-maintainer badge surfaced on
the public cookbook page + the discover/leaderboard feeds. Assignable by an
admin/master action (POST /api/cookbooks/{id}/verify). Default false.

DOWNGRADE: drop the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "spotify_0608_g_verified"
down_revision = "spotify_0608_e_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cookbooks",
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("cookbooks", "is_verified")
