"""converge_0208 P1 — record existing pin INTENT in bundle_skills.pin_mode.

Revision ID: c0208_p1_pin_intent
Revises: a2ed83d0e3b6
Create Date: 2026-08-03

``bundle_skills.pin_mode`` arrived with the spotify_1507 bundle lock and the
lock resolver has read it ever since — 'pin' freezes to ``pinned_version``,
'track' follows the published head. But NOTHING ever wrote the column. The pin
route (``PATCH /api/bundles/{id}/skills/{slug}/pin``) set ``pinned_version`` and
``source='overridden'`` and left ``pin_mode`` on its ``server_default='track'``,
so every row in production is 'track' and the column has never meant anything.

That did not matter while reconcile resolved off ``pinned_version`` directly.
converge_0208 P1 routes reconcile through the lock, which decides on
``pin_mode`` — so without this migration every deliberate pin an owner has ever
set would silently start tracking the head on deploy.

The intent is recoverable because the two writers are distinguishable:

  source='overridden' + pinned_version  → the pin route ran. A deliberate pin.
  any other source    + pinned_version  → bookkeeping residue. reconcile-apply
                                          and loopskill_sync write
                                          ``pinned_version`` on every UPDATE
                                          row to record what they last shipped;
                                          the owner never asked for a pin. This
                                          is precisely the residue that made
                                          reconcile target dead versions on
                                          tori-core, and it is left as 'track'
                                          so the entry follows the live head.

Data-only. Idempotent. The down-migration returns every row to 'track', which
is the exact state before this ran.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c0208_p1_pin_intent"
down_revision: str | Sequence[str] | None = "a2ed83d0e3b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE bundle_skills
           SET pin_mode = 'pin'
         WHERE source = 'overridden'
           AND pinned_version IS NOT NULL
           AND pin_mode <> 'pin'
        """
    )


def downgrade() -> None:
    op.execute("UPDATE bundle_skills SET pin_mode = 'track' WHERE pin_mode = 'pin'")
