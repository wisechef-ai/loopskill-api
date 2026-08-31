"""bundles0811 merge P3.5 + P3.6 heads

Revision ID: 06671beefcd1
Revises: b0811_p35_fed_propose, bundles0811_p36_hub_license
Create Date: 2026-08-11 01:57:35.646297

No-op merge. P3.5 (federation_registry_proposals) and P3.6
(federation_hub_skills.license) were authored in parallel worktrees and each
CORRECTLY chained onto the same parent, ``mesh0408_w2_sub_event_at``. Both were
single-head in isolation, so neither branch's CI could see the collision — it
only appeared once the second one merged, and then `alembic upgrade head` failed
repo-wide with "Multiple head revisions are present".

Neither migration touches the other's table, so there is nothing to reconcile:
this revision exists purely to rejoin the two lineages into one head.
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "06671beefcd1"
down_revision: Union[str, Sequence[str], None] = (
    "b0811_p35_fed_propose",
    "bundles0811_p36_hub_license",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rejoin the two lineages. No schema change."""


def downgrade() -> None:
    """Re-split into two heads. No schema change."""
