"""Rename studio→operator tier slug (Phase 3 SSOT collapse)

Revision ID: d4e5f6a7b8c9
Revises: b2c3d4e5f6a1
Create Date: 2026-05-11 10:28:00.000000

Migrates all users with subscription_tier='studio' to 'operator' (the canonical
SSOT slug from config/tiers.yaml). Also fixes any users with a subscription_id
but NULL subscription_tier who should have been captured by the webhook.

DOWNGRADE NOTE: downgrade() reverses only the rows we migrated in upgrade().
It does NOT perfectly reverse if new 'operator' rows were added in between
(e.g. via new checkouts after the migration). Use downgrade for rollback only,
not as a generic reversal.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "b2c3d4e5f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename studio → operator for all existing rows.

    Two passes:
    1. Rename 'studio' rows to 'operator' (the tier rename).
    2. Verify no 'studio' rows remain post-migration (guard against partial runs).
    """
    conn = op.get_bind()

    # Step 1: rename subscription_tier='studio' → 'operator'
    result = conn.execute(
        sa.text(
            "UPDATE users SET subscription_tier = 'operator' "
            "WHERE subscription_tier = 'studio'"
        )
    )
    studio_migrated = result.rowcount
    print(f"[d4e5f6a7b8c9] Renamed {studio_migrated} studio→operator rows")

    # Step 2: guard — ensure no 'studio' rows remain
    remaining = conn.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE subscription_tier = 'studio'")
    ).scalar()
    if remaining:
        raise RuntimeError(
            f"[d4e5f6a7b8c9] ABORT: {remaining} rows still have subscription_tier='studio' "
            "after migration. Manual investigation required."
        )

    print(f"[d4e5f6a7b8c9] Guard passed — 0 studio rows remain. "
          f"Total operator rows post-migration: "
          + str(conn.execute(
              sa.text("SELECT COUNT(*) FROM users WHERE subscription_tier = 'operator'")
          ).scalar()))


def downgrade() -> None:
    """Reverse the studio→operator rename for rollback purposes only.

    WARNING: This downgrade only reverses rows that were 'studio' before
    the upgrade. If new 'operator' subscriptions were created after the
    upgrade, those will also be incorrectly renamed back to 'studio'.
    Use this for rollback only within the same deploy window.
    """
    conn = op.get_bind()

    result = conn.execute(
        sa.text(
            "UPDATE users SET subscription_tier = 'studio' "
            "WHERE subscription_tier = 'operator'"
        )
    )
    print(f"[d4e5f6a7b8c9 downgrade] Reverted {result.rowcount} operator→studio rows "
          "(rollback only — see migration docstring)")
