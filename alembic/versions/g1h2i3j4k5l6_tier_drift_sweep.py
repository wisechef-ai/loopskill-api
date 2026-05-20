"""tier_drift_sweep — Phase G (recipes_2005/G)

Revision ID: g1h2i3j4k5l6
Revises: c4d5e6f7a8b9
Create Date: 2026-05-20 13:00:00.000000

Phase G of recipes_2005 sprint: align DB skill tier slugs with Phase 5 canonical names.

UPGRADE:
  UPDATE skills SET tier='pro'      WHERE tier='cook'     AND is_archived=false
  UPDATE skills SET tier='pro_plus' WHERE tier='operator' AND is_archived=false

  Defensive guard: verify post-update no live (non-archived) skill has tier
  IN ('cook','operator','studio').

DOWNGRADE (best-effort):
  Reverts pro→cook and pro_plus→operator on non-archived rows.
  WARNING: If new 'pro'/'pro_plus' skills were added after upgrade, those will
  also be incorrectly reverted. Use only within the same deploy window.
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "g1h2i3j4k5l6"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    """Rename live skill tier slugs: cook→pro and operator→pro_plus.

    Three passes:
    1. Rename 'cook' → 'pro' (non-archived only).
    2. Rename 'operator' → 'pro_plus' (non-archived only).
    3. Defensive guard — assert zero live rows with tier IN ('cook','operator','studio').
    """
    conn = op.get_bind()

    # Step 1: cook → pro (non-archived skills only)
    result = conn.execute(
        sa.text(
            "UPDATE skills SET tier = 'pro' "
            "WHERE tier = 'cook' AND is_archived = false"
        )
    )
    cook_migrated = result.rowcount
    msg = f"[g1h2i3j4k5l6] tier_drift_sweep: renamed {cook_migrated} cook→pro rows (non-archived)"
    print(msg)
    logger.info(msg)

    # Step 2: operator → pro_plus (non-archived skills only)
    result = conn.execute(
        sa.text(
            "UPDATE skills SET tier = 'pro_plus' "
            "WHERE tier = 'operator' AND is_archived = false"
        )
    )
    operator_migrated = result.rowcount
    msg = f"[g1h2i3j4k5l6] tier_drift_sweep: renamed {operator_migrated} operator→pro_plus rows (non-archived)"
    print(msg)
    logger.info(msg)

    # Step 3: defensive guard — no live skills should have legacy tier slugs
    # (archived rows intentionally left alone to preserve history)
    remaining = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM skills "
            "WHERE tier IN ('cook', 'operator', 'studio') "
            "AND is_archived = false"
        )
    ).scalar()

    if remaining:
        raise RuntimeError(
            f"[g1h2i3j4k5l6] ABORT: {remaining} live (non-archived) skills still have "
            "tier IN ('cook','operator','studio') after migration. "
            "Manual investigation required — do NOT proceed."
        )

    pro_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM skills WHERE tier = 'pro' AND is_archived = false")
    ).scalar()
    pro_plus_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM skills WHERE tier = 'pro_plus' AND is_archived = false")
    ).scalar()
    free_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM skills WHERE tier = 'free' AND is_archived = false")
    ).scalar()

    summary = (
        f"[g1h2i3j4k5l6] tier_drift_sweep upgrade complete — "
        f"migrated: cook→pro={cook_migrated}, operator→pro_plus={operator_migrated}. "
        f"Live skill counts: free={free_count}, pro={pro_count}, pro_plus={pro_plus_count}."
    )
    print(summary)
    logger.info(summary)


def downgrade() -> None:
    """Reverse pro→cook and pro_plus→operator for non-archived rows (best-effort).

    WARNING: This is a best-effort rollback ONLY for use within the same deploy
    window. Any 'pro'/'pro_plus' skills created AFTER the upgrade will be
    incorrectly renamed back to 'cook'/'operator'.
    """
    conn = op.get_bind()

    warning = (
        "[g1h2i3j4k5l6 downgrade] WARNING: reverting tier_drift_sweep. "
        "Skills added after the upgrade may be incorrectly renamed. "
        "Use only within the same deploy window."
    )
    print(warning)
    logger.warning(warning)

    result = conn.execute(
        sa.text(
            "UPDATE skills SET tier = 'cook' "
            "WHERE tier = 'pro' AND is_archived = false"
        )
    )
    msg = f"[g1h2i3j4k5l6 downgrade] Reverted {result.rowcount} pro→cook rows (non-archived)"
    print(msg)
    logger.info(msg)

    result = conn.execute(
        sa.text(
            "UPDATE skills SET tier = 'operator' "
            "WHERE tier = 'pro_plus' AND is_archived = false"
        )
    )
    msg = f"[g1h2i3j4k5l6 downgrade] Reverted {result.rowcount} pro_plus→operator rows (non-archived)"
    print(msg)
    logger.info(msg)
