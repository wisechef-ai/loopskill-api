"""spotify_2607: merge Phase 0 + Phase A heads

Revision ID: a2ed83d0e3b6
Revises: sp2607_0_owner_handle, spotify2607_a_liked_federated
Create Date: 2026-07-26 15:56:16.328962

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2ed83d0e3b6'
down_revision: Union[str, Sequence[str], None] = ('sp2607_0_owner_handle', 'spotify2607_a_liked_federated')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
