"""stabilization_2605: merge fleet+intent-survey with referral heads

Revision ID: 7da7d5cc45e8
Revises: a1c2d3e4f5g6, d7a3c1f9e201
Create Date: 2026-05-03 18:50:28.862517

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7da7d5cc45e8'
down_revision: Union[str, Sequence[str], None] = ('a1c2d3e4f5g6', 'd7a3c1f9e201')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
