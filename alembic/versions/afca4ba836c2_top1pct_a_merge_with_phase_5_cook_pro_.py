"""top1pct A — merge with Phase 5 cook→pro rename

Revision ID: afca4ba836c2
Revises: e1f2a3b4c5d6, e1f5a3c2b7d0
Create Date: 2026-05-11 20:36:28.492634

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'afca4ba836c2'
down_revision: Union[str, Sequence[str], None] = ('e1f2a3b4c5d6', 'e1f5a3c2b7d0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
