"""category backfill null — infer category on uncategorized skills

Revision ID: f8ade9aa1b68
Revises: 7c51d9bc2d36
Create Date: 2026-07-16

atomic-habits 2026-07-16 rank-1: GET /api/stats (verified_at 2026-07-16) shows
by_category = [uncategorized:48, data:2, ops:2, automation:1] out of
total_skills:53 — 48/53 (90%) of the catalog has category=NULL.
app/skill_routes.py L133-146 already widened the literal search pass to also
match Skill.category/readme, but that widening has nothing to match against
for 90% of rows — measured live: q=ops returned 1 hit while 12 skills are
*actually* ops-shaped, q=devops returned 0 while 2 are devops-shaped. The
literal-pass fix already shipped; this migration ships the missing DATA half.

Uses app.services.category_infer.classify_category (keyword-bucket
classifier authored directly from docs/taxonomy.md's canonical-10 + its
legacy-mapping table — same SSOT as the b3c4d5e6f701 taxonomy migration, zero
new vocabulary invented) to assign one of the 10 canonical buckets to every
row where category IS NULL, using title + slug + description + readme as
signal. Anything unmatched falls back to "productivity" per taxonomy.md's
documented lowest-risk default — so after this migration, `category` is never
NULL for a public skill again.

Touches ONLY skills.category. No tier, no Stripe, no pricing. Additive/
idempotent: re-running only affects rows still NULL (there are none after a
successful run), so it's safe to replay.

Downgrade is intentionally a no-op — reconstructing "was NULL before" is
lossy and pointless (the whole point is these rows had no signal). Restore
from backup if a true reverse is ever needed, matching the documented
precedent in b3c4d5e6f701's downgrade().
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

from app.services.category_infer import classify_category

# revision identifiers, used by Alembic.
revision: str = "f8ade9aa1b68"
down_revision: Union[str, Sequence[str], None] = "7c51d9bc2d36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Backfill category on every row where it is currently NULL."""
    conn = op.get_bind()

    rows = conn.execute(
        sa.text(
            "SELECT id, title, slug, description, readme FROM skills "
            "WHERE category IS NULL"
        )
    ).fetchall()

    for row in rows:
        inferred = classify_category(
            title=row.title,
            description=row.description,
            slug=row.slug,
            readme=row.readme,
        )
        conn.execute(
            sa.text("UPDATE skills SET category = :cat WHERE id = :id"),
            {"cat": inferred, "id": row.id},
        )


def downgrade() -> None:
    # Reconstructing "was NULL" is lossy by design (these rows had zero
    # signal before this migration) — no-op, matching the precedent set by
    # b3c4d5e6f701's downgrade(). Restore from backup if a true reverse is
    # ever required.
    pass
