"""atomic-habits 2026-08-03 rank-8 REVENUE/CATALOG — backfill discovery tags
for repo-stewardship-pack (the only untagged composite loop).

Revision ID: ah0803_repo_pack_tags
Revises: c0208_p1_pin_intent
Create Date: 2026-08-03

Live evidence (verified 2026-08-03T07:07+02:00): GET
https://app.loopskill.io/api/composite-loops returns 3 composite loops —
atomic-habits and dreaming both carry 5 discovery tags (ah0723 backfill),
repo-stewardship-pack ships tags=[]. Because the tag filter is a real
server-side filter (composite_loop_routes.py:97-98) and the portal chip row
is built client-side from item.tags (browse.astro:588-600), the zero-tag
pack renders no chip and is excluded from every tag-filtered browse view —
/api/composite-loops?tag=agent-ops returns 2 of 3, silently dropping the
most commercial pack (bundles repo-steward + test-green + pr-review behind
one 30m-scheduled deploy).

Mirrors ah0723_composite_loop_tags exactly (additive, idempotent,
backfill-by-slug). Catalog metadata only — no tier/Stripe/pricing SSOT
touched. Downgrade is a no-op (the tags column already exists from ah0723;
this migration only UPDATEs a row, nothing to structurally revert).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ah0803_repo_pack_tags"
down_revision: Union[str, Sequence[str], None] = "c0208_p1_pin_intent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tags derived from the pack's actual composition/schedule (not invented):
# it bundles repo-steward + test-green + pr-review behind a 30m schedule
# with verifier repo-steward-loop.
REPO_PACK_TAGS: list[str] = ["agent-ops", "ci", "github", "code-quality", "scheduled"]


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE composite_loops SET tags = :tags WHERE slug = :slug"),
        {"tags": json.dumps(REPO_PACK_TAGS), "slug": "repo-stewardship-pack"},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE composite_loops SET tags = :tags WHERE slug = :slug"),
        {"tags": json.dumps([]), "slug": "repo-stewardship-pack"},
    )
