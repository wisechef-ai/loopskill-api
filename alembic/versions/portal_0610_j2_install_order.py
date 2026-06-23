"""portal_0610/J2 — cookbook_skills.install_order for Composer reorder

Revision ID: portal_0610_j2_install_order
Revises: spotify_0608_g_verified
Create Date: 2026-06-10 15:30:00.000000

portal_0610 J2 (L3 — the multi-select Composer). The Composer lets an operator
REORDER the skills in a cookbook (drag/up-down); install + manifest emit in that
order. CookbookSkill had no ordering column (only the separate CookbookDeployment
table did), so add a nullable-default integer `install_order` to
`cookbook_skills`. Default 100 matches CookbookDeployment's convention so mixed
rows sort sanely; ties fall back to added_at via the route's ORDER BY.

DOWNGRADE: drop the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "portal_0610_j2_install_order"
down_revision = "spotify_0608_g_verified"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cookbook_skills",
        sa.Column("install_order", sa.Integer(), nullable=False, server_default=sa.text("100")),
    )
    op.create_index(
        "ix_cookbook_skills_order",
        "cookbook_skills",
        ["cookbook_id", "install_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_cookbook_skills_order", table_name="cookbook_skills")
    op.drop_column("cookbook_skills", "install_order")
