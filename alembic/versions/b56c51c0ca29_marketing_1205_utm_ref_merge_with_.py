"""marketing_1205 utm_ref merge with sibling

Revision ID: b56c51c0ca29
Revises: 812c8f26fb4d, c4679433229e
Create Date: 2026-05-12 12:45:22.343380

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b56c51c0ca29'
down_revision: Union[str, Sequence[str], None] = ('812c8f26fb4d', 'c4679433229e')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
