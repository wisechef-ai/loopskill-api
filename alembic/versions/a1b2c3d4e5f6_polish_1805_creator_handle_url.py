"""polish_1805 — creator handle + url for author identity

Adds two nullable columns to the ``creators`` table so the portal can render
"by <name> @<handle>" on every skill page (polish_1805 item 4). Backfilled
by scripts/backfill_creator_identity.py which scans cookbook frontmatter,
SKILL.md ``maintainer:`` field, and git author info to populate handles.

Both columns nullable — existing rows are unaffected and continue to render
as plain "by <name>" until the backfill cron reaches them.

Revision ID: a1b2c3d4e5f6
Revises: e3f4a5b6c7d8
Create Date: 2026-05-17 19:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add ``handle`` and ``url`` columns to ``creators``."""
    op.add_column("creators", sa.Column("handle", sa.String(length=64), nullable=True))
    op.add_column("creators", sa.Column("url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("creators", "url")
    op.drop_column("creators", "handle")
