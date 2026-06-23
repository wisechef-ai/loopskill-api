"""baseline

Revision ID: 4ba0bf05cd47
Revises:
Create Date: 2026-04-28 20:54:05.110493

Baseline stamp matching the production schema as of Sprint 4.
Production tables (confirmed):
  - telemetry_events: id, event_type, skill_slug, payload (text), client_ip, created_at
  - install_events:   id, skill_id, skill_slug, api_key_id, version_semver, client_ip, created_at
  - skills:           id, slug, title, description, category, readme, license, tier,
                      is_public, creator_id, org_id, created_at, updated_at
  - carousel_entries: id, skill_id, featured_date, tagline, position, created_at

This revision is intentionally a no-op.  It is used as the alembic stamp
target on production before applying the next revision:

    alembic stamp 4ba0bf05cd47
    alembic upgrade head
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4ba0bf05cd47'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op — baseline stamp only."""
    pass


def downgrade() -> None:
    """No-op — nothing to reverse."""
    pass
