"""atomic-habits 2026-07-23 rank-8 REVENUE/CATALOG — add composite_loops.tags,
backfill discovery/category tags for atomic-habits + dreaming.

Revision ID: ah0723_composite_loop_tags
Revises: ah0721_composite_loop_ver
Create Date: 2026-07-23

atomic-habits 2026-07-23 rank-8: GET /api/loops/packs/self-improvement (live,
verified_at 2026-07-23T07:09) shows both self-improvement pack members —
atomic-habits + dreaming — returning tags='?' (no tags column exists on
composite_loops at all, unlike the old `loops` table which got this fix in
ah0719_loop_tags). Untagged loops have zero discovery surface outside a
direct slug lookup or the manually-curated self-improvement pack. Catalog
metadata only — no tier/Stripe/pricing SSOT touched.

Mirrors ah0719_loop_tags_backfill.py exactly (same additive JSON column +
backfill-by-slug pattern), applied to the NEW composite_loops surface.

Additive/idempotent: only touches rows whose slug is in COMPOSITE_LOOP_TAGS;
re-running is a no-op UPDATE (same values). No-ops if a row doesn't exist in
this DB (fresh CI/test environments). Safe to replay.

Downgrade drops the column — tags are pure discovery metadata, no data loss
risk to any other table.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ah0723_composite_loop_tags"
down_revision: Union[str, Sequence[str], None] = "ah0721_composite_loop_ver"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# slug -> discovery tags, derived from each loop's actual prompt/schedule/
# verifier behavior (not invented — read off what each loop's own
# description already claims to do).
COMPOSITE_LOOP_TAGS: dict[str, list[str]] = {
    "atomic-habits": ["self-improvement", "agent-ops", "daily", "compounding", "scheduled"],
    "dreaming": ["self-improvement", "memory", "consolidation", "agent-ops", "scheduled"],
}


def upgrade() -> None:
    conn = op.get_bind()
    op.add_column("composite_loops", sa.Column("tags", sa.JSON(), nullable=True))

    for slug, tags in COMPOSITE_LOOP_TAGS.items():
        # json.dumps, not the raw list: SQLite's driver can't bind a Python
        # list directly (mirrors ah0719's fix for the same trap on `loops`).
        conn.execute(
            sa.text("UPDATE composite_loops SET tags = :tags WHERE slug = :slug"),
            {"tags": json.dumps(tags), "slug": slug},
        )


def downgrade() -> None:
    op.drop_column("composite_loops", "tags")
