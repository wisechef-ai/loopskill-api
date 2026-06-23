"""Rename cook→pro and operator→pro_plus tier slugs (Phase 5 slug parity)

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-05-11 12:00:00.000000

Phase 5 of RCP-INCIDENT-2026-05-11: align DB tier slugs with display names.
  cook     → pro       (was display_name: Pro)
  operator → pro_plus  (was display_name: Pro+)

DOWNGRADE NOTE: downgrade() reverses only the cook→pro and operator→pro_plus
renames. It does NOT touch 'studio' rows — those are owned by the previous
migration (d4e5f6a7b8c9) and must be kept separate for clean rollback ordering.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename cook → pro and operator → pro_plus for all existing rows.

    Three passes:
    1. Rename 'cook' rows to 'pro'.
    2. Rename 'operator' rows to 'pro_plus'.
    3. Guard — assert zero rows with tier IN ('cook','operator','studio')
       post-migration. 'studio' should already be gone from Phase 3, but we
       double-check here to catch any stale rows.
    """
    conn = op.get_bind()

    # Step 1: cook → pro
    result = conn.execute(
        sa.text(
            "UPDATE users SET subscription_tier = 'pro' "
            "WHERE subscription_tier = 'cook'"
        )
    )
    cook_migrated = result.rowcount
    print(f"[e1f2a3b4c5d6] Renamed {cook_migrated} cook→pro rows")

    # Step 2: operator → pro_plus
    result = conn.execute(
        sa.text(
            "UPDATE users SET subscription_tier = 'pro_plus' "
            "WHERE subscription_tier = 'operator'"
        )
    )
    operator_migrated = result.rowcount
    print(f"[e1f2a3b4c5d6] Renamed {operator_migrated} operator→pro_plus rows")

    # Step 3: guard — ensure no legacy slugs remain
    remaining = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM users "
            "WHERE subscription_tier IN ('cook', 'operator', 'studio')"
        )
    ).scalar()
    if remaining:
        raise RuntimeError(
            f"[e1f2a3b4c5d6] ABORT: {remaining} rows still have legacy "
            "subscription_tier IN ('cook','operator','studio') after migration. "
            "Manual investigation required."
        )

    pro_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE subscription_tier = 'pro'")
    ).scalar()
    pro_plus_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM users WHERE subscription_tier = 'pro_plus'")
    ).scalar()
    print(
        f"[e1f2a3b4c5d6] Guard passed — 0 legacy rows remain. "
        f"pro={pro_count}, pro_plus={pro_plus_count}"
    )


def downgrade() -> None:
    """Reverse the pro→cook and pro_plus→operator renames (rollback only).

    WARNING: Only reverses rows that were migrated by THIS migration.
    Does NOT touch 'studio' rows — those belong to migration d4e5f6a7b8c9.
    If new 'pro' / 'pro_plus' subscriptions were added after the upgrade,
    those will also be incorrectly renamed back. Use for rollback only
    within the same deploy window.
    """
    conn = op.get_bind()

    result = conn.execute(
        sa.text(
            "UPDATE users SET subscription_tier = 'cook' "
            "WHERE subscription_tier = 'pro'"
        )
    )
    print(f"[e1f2a3b4c5d6 downgrade] Reverted {result.rowcount} pro→cook rows")

    result = conn.execute(
        sa.text(
            "UPDATE users SET subscription_tier = 'operator' "
            "WHERE subscription_tier = 'pro_plus'"
        )
    )
    print(f"[e1f2a3b4c5d6 downgrade] Reverted {result.rowcount} pro_plus→operator rows")
