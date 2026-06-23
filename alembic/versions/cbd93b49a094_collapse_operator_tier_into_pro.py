"""Collapse operator-tier skills into cook (Pro) tier.

Adam's directive 2026-05-13: all paid skills become available on Pro ($20/mo).
Pro+ keeps its non-skill perks (20 cookbooks, scoped API keys, private
catalog, fleet deploy, priority review) but no longer holds exclusive skills.

Before: free=3, cook=36 (Pro), operator=11 (Pro+ exclusives).
After:  free=3, cook=47 (Pro), operator=0.

This is a one-way data migration of `skills.tier` only. The DB slug `operator`
remains a valid value (no CHECK constraint on tier, kept as legacy alias in
app.routes.TIER_RANK) so future rows could theoretically use it again, but
the catalog will not contain any until Adam reverses this call.

Down-migration is intentionally a no-op: we have no record of WHICH skills
were operator-tier before, and re-promoting them would be a product decision,
not a schema rollback. Git history of this commit + the prior
config/recipes-marketing.yaml snapshot serve as the recoverable trail.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "cbd93b49a094"
down_revision: Union[str, Sequence[str], None] = "b56c51c0ca29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Promote all live operator-tier skills to cook (Pro tier).

    Limited to is_archived=false so we don't disturb soft-archived history.
    """
    bind = op.get_bind()
    result = bind.execute(
        sa.text(
            "UPDATE skills SET tier = 'cook' "
            "WHERE tier = 'operator' AND is_archived = false"
        )
    )
    # rowcount surfaces in alembic upgrade output for audit
    print(
        f"[cbd93b49a094] promoted {result.rowcount} operator-tier "
        f"skill(s) to cook (Pro)"
    )


def downgrade() -> None:
    """Intentional no-op. See module docstring."""
    pass
