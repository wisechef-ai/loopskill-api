"""top1pct C merge — combine Phase A merge head with Phase C api-key cookbook scoping

Revision ID: 812c8f26fb4d
Revises: afca4ba836c2, f2a3b4c5d6e7
Create Date: 2026-05-11 21:19:57.448432

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '812c8f26fb4d'
down_revision: Union[str, Sequence[str], None] = ('afca4ba836c2', 'f2a3b4c5d6e7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
