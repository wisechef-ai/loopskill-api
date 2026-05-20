"""tier_drift_sweep_archived — Phase G follow-up (recipes_2005/G+)

Revision ID: h2i3j4k5l6m7
Revises: 6250d327ebf7
Create Date: 2026-05-20 19:30:00.000000

Phase G+ of recipes_2005 sprint: complete the tier slug migration by sweeping
ARCHIVED rows that the original g1h2i3j4k5l6 sweep intentionally skipped.

CONTEXT:
  g1h2i3j4k5l6 (the initial Phase G sweep) deliberately scoped to non-archived
  rows because the team wanted to "preserve history." That choice leaves a
  durability gap: after the 30-day legacy READ-alias window closes on
  2026-06-10, archived rows whose tier is still 'cook'/'operator'/'studio'
  become orphans relative to the canonical tier vocabulary (free/pro/pro_plus).

  Auditing prod on 2026-05-20 found 15 such rows:
    cook     | archived=12  (-> pro)
    operator | archived= 3  (-> pro_plus)

  Those rows still feed:
    - the marketing tier-count belt-and-suspenders fallback in marketing_routes.py
    - admin restoration flows (un-archive)
    - any historical reporting joins on `tier`

  The right fix is to use the canonical slugs for ALL rows; archival status is
  a separate axis. Tier history (if anyone needs to know "this was originally a
  Cook-tier skill in 2025") lives in git, deploy notes, and skill_versions.

UPGRADE:
  UPDATE skills SET tier='pro'      WHERE tier='cook'                AND is_archived=true
  UPDATE skills SET tier='pro_plus' WHERE tier IN ('operator','studio') AND is_archived=true
  Defensive guard: assert zero rows with tier IN ('cook','operator','studio')
  anywhere (archived or not) post-update.

DOWNGRADE:
  Best-effort reversal of pro→cook and pro_plus→operator on ARCHIVED rows only.
  Same caveats as g1h2i3j4k5l6 downgrade: a new pro/pro_plus archived row added
  after upgrade will be incorrectly renamed by the downgrade. Use only within
  the same deploy window.
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "h2i3j4k5l6m7"
down_revision: Union[str, None] = "6250d327ebf7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger(__name__)


def upgrade() -> None:
    """Rename archived skill tier slugs: cook→pro and operator/studio→pro_plus.

    Three passes:
      1. Archived 'cook'   → 'pro'.
      2. Archived 'operator' or 'studio' → 'pro_plus'.
      3. Defensive guard — assert no rows (archived or not) still have tier
         IN ('cook','operator','studio') anywhere in the table.
    """
    conn = op.get_bind()

    # Step 1: archived cook → pro
    result = conn.execute(
        sa.text(
            "UPDATE skills SET tier = 'pro' "
            "WHERE tier = 'cook' AND is_archived = true"
        )
    )
    cook_arch = result.rowcount
    msg = f"[h2i3j4k5l6m7] tier_drift_sweep_archived: renamed {cook_arch} archived cook→pro rows"
    print(msg)
    logger.info(msg)

    # Step 2: archived operator/studio → pro_plus
    result = conn.execute(
        sa.text(
            "UPDATE skills SET tier = 'pro_plus' "
            "WHERE tier IN ('operator', 'studio') AND is_archived = true"
        )
    )
    op_arch = result.rowcount
    msg = f"[h2i3j4k5l6m7] tier_drift_sweep_archived: renamed {op_arch} archived operator/studio→pro_plus rows"
    print(msg)
    logger.info(msg)

    # Step 3: defensive guard — no row should still carry a legacy slug
    remaining = conn.execute(
        sa.text(
            "SELECT COUNT(*) FROM skills "
            "WHERE tier IN ('cook', 'operator', 'studio')"
        )
    ).scalar()

    if remaining:
        raise RuntimeError(
            f"[h2i3j4k5l6m7] ABORT: {remaining} skills still have "
            "tier IN ('cook','operator','studio') after migration. "
            "Manual investigation required — do NOT proceed."
        )

    # Final state report (canonical slugs only)
    rows = conn.execute(
        sa.text(
            "SELECT tier, COUNT(*) FILTER (WHERE NOT is_archived) AS live, "
            "       COUNT(*) FILTER (WHERE is_archived) AS archived "
            "FROM skills GROUP BY tier ORDER BY tier"
        )
    ).fetchall()
    summary_lines = [
        f"[h2i3j4k5l6m7] tier_drift_sweep_archived upgrade complete. Canonical-only state:"
    ]
    for tier, live, archived in rows:
        summary_lines.append(f"    tier={tier!r}: live={live}, archived={archived}")
    summary = "\n".join(summary_lines)
    print(summary)
    logger.info(summary)


def downgrade() -> None:
    """Reverse pro→cook and pro_plus→operator for ARCHIVED rows (best-effort).

    WARNING: Same caveat as g1h2i3j4k5l6 downgrade. Any 'pro'/'pro_plus'
    ARCHIVED rows created AFTER the upgrade will be incorrectly renamed.
    Use only within the same deploy window.
    """
    conn = op.get_bind()

    warning = (
        "[h2i3j4k5l6m7 downgrade] WARNING: reverting tier_drift_sweep_archived. "
        "Archived skills added after the upgrade may be incorrectly renamed. "
        "Use only within the same deploy window."
    )
    print(warning)
    logger.warning(warning)

    result = conn.execute(
        sa.text(
            "UPDATE skills SET tier = 'cook' "
            "WHERE tier = 'pro' AND is_archived = true"
        )
    )
    msg = f"[h2i3j4k5l6m7 downgrade] Reverted {result.rowcount} archived pro→cook rows"
    print(msg)
    logger.info(msg)

    result = conn.execute(
        sa.text(
            "UPDATE skills SET tier = 'operator' "
            "WHERE tier = 'pro_plus' AND is_archived = true"
        )
    )
    msg = f"[h2i3j4k5l6m7 downgrade] Reverted {result.rowcount} archived pro_plus→operator rows"
    print(msg)
    logger.info(msg)
