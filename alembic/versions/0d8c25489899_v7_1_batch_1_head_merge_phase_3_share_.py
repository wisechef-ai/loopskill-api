"""v7.1 batch-1 head merge: phase-3 share_tokens + phase-4 search_vector

Revision ID: 0d8c25489899
Revises: a3f1e9b5c7d2, b2f4d8e9a1c3
Create Date: 2026-05-07 16:10:48.082610

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0d8c25489899'
down_revision: Union[str, Sequence[str], None] = ('a3f1e9b5c7d2', 'b2f4d8e9a1c3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
