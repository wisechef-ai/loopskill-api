"""atomic-habits 2026-07-19 rank-8 REVENUE/CATALOG — add loops.tags, backfill 10 starter loops

Revision ID: ah0719_loop_tags
Revises: f8ade9aa1b68
Create Date: 2026-07-19

atomic-habits 2026-07-19 rank-8: GET /api/loops (verified_at 2026-07-19) shows
all 10 runnable loops carry only a single `category` (development/productivity/
data/security/examples) and NO discovery tags — they don't surface under
topic/tag search on app.loopskill.io. Catalog metadata only: no tier SSOT, no
Stripe, no pricing — fallback-executable. Discoverability -> runs -> the
rank-1 install bridge (this same session, schemas.py VerifierRunOut) -> pro
conversion.

Adds `loops.tags` (JSON array of strings, nullable — NULL treated as [] by
the API layer) and backfills the 10 starter loops from
scripts/seed_starter_catalog.py with topic tags matching their actual
verification behavior.

Additive/idempotent: only touches rows whose slug matches the known starter
set; re-running is a no-op UPDATE (same values). Safe to replay.

Downgrade drops the column — tags are pure discovery metadata, no data loss
risk to any other table.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ah0719_loop_tags"
down_revision: Union[str, Sequence[str], None] = "f8ade9aa1b68"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# slug -> discovery tags, derived from each loop's actual verification_script
# behavior in scripts/seed_starter_catalog.py (not invented — read off what
# each loop's category + description already claim to do).
LOOP_TAGS: dict[str, list[str]] = {
    "hello-world-loop": ["examples", "agent-ops", "getting-started"],
    "pr-review-loop": ["review", "ci", "agent-ops", "github"],
    "daily-briefing-loop": ["agent-ops", "reporting", "scheduled"],
    "test-green-loop": ["testing", "ci", "agent-ops"],
    "lint-clean-loop": ["ci", "agent-ops", "code-quality"],
    "secret-scan-loop": ["security", "ci", "agent-ops"],
    "changelog-from-commits-loop": ["docs", "ci", "agent-ops", "github"],
    "doc-coverage-loop": ["docs", "code-quality", "agent-ops"],
    "json-schema-validate-loop": ["data-validation", "ci", "agent-ops"],
    "repo-steward-loop": ["agent-ops", "ci", "code-quality", "github"],
}


def upgrade() -> None:
    conn = op.get_bind()
    op.add_column("loops", sa.Column("tags", sa.JSON(), nullable=True))

    for slug, tags in LOOP_TAGS.items():
        conn.execute(
            sa.text("UPDATE loops SET tags = :tags WHERE slug = :slug"),
            {"tags": tags, "slug": slug},
        )


def downgrade() -> None:
    op.drop_column("loops", "tags")
