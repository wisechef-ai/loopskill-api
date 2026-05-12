"""marketing_1205 utm_ref merge

Revision ID: c32b244d5441
Revises: e1f2a3b4c5d6, f2a3b4c5d6e7
Create Date: 2026-05-12 14:36:09.228752

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c32b244d5441'
down_revision: Union[str, Sequence[str], None] = ('e1f2a3b4c5d6', 'f2a3b4c5d6e7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
