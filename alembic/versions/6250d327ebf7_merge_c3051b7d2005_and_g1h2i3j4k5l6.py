"""merge_c3051b7d2005_and_g1h2i3j4k5l6

Revision ID: 6250d327ebf7
Revises: c3051b7d2005, g1h2i3j4k5l6
Create Date: 2026-05-20 16:36:44.896621

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6250d327ebf7'
down_revision: Union[str, Sequence[str], None] = ('c3051b7d2005', 'g1h2i3j4k5l6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
