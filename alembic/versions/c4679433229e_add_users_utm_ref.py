"""add users utm_ref

marketing_1205: UTM ref attribution column on users table.
Tracks which platform drove the signup (?ref=li|x|yt|ig|fb|agentpact).

Revision ID: c4679433229e
Revises: c32b244d5441
Create Date: 2026-05-12 14:36:09.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4679433229e"
down_revision: Union[str, Sequence[str], None] = "c32b244d5441"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("utm_ref", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "utm_ref")
