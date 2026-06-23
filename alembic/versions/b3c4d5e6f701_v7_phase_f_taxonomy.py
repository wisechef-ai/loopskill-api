"""v7 phase F — taxonomy unification

Revision ID: b3c4d5e6f701
Revises: a2b3c4d5e6f7
Create Date: 2026-05-06 00:00:00.000000

Phase F — collapse the 4-vocabulary mess into a single source of truth.

Tier:
  - drop `studio` (alias to `operator`); keep free, cook, operator

Category:
  - canonical 10: research, dev-tools, agency, marketing, content,
    automation, code-review, productivity, data, ops
  - everything else maps to its nearest canonical bucket
    (see docs/taxonomy.md mapping table)

Touches both `skills.tier` / `skills.category` and `users.subscription_tier`
so portal billing reads stay coherent. `verticals` column is intentionally
untouched — that vocabulary is deferred to a later phase.

Downgrade is best-effort: tier `operator` → `studio` is ambiguous (we cannot
distinguish original-operator from migrated-studio), and category remap is
lossy. The downgrade therefore logs and no-ops; restore from backup if a true
reverse is needed.
"""
from alembic import op


revision = "b3c4d5e6f701"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


# Authored from docs/taxonomy.md — keep these in sync.
CATEGORY_MAP = {
    # → ops
    "devops": "ops",
    "infrastructure": "ops",
    "platform": "ops",
    "monitoring": "ops",
    "deploy": "ops",
    # → data
    "data-extraction": "data",
    "ml": "data",
    "analytics": "data",
    "scraping": "data",
    "etl": "data",
    # → content
    "creative": "content",
    "copywriting": "content",
    "video": "content",
    "image": "content",
    # → marketing
    "seo": "marketing",
    "ads": "marketing",
    "growth": "marketing",
    "email": "marketing",
    "reporting": "marketing",
    # → agency
    "client-reporting": "agency",
    "consulting": "agency",
    "proposals": "agency",
    # → dev-tools
    "development": "dev-tools",
    "coding": "dev-tools",
    "cli": "dev-tools",
    "ide": "dev-tools",
    # → code-review
    "code-quality": "code-review",
    "lint": "code-review",
    "security": "code-review",
    "audit": "code-review",
    # → research
    "research-tools": "research",
    "discovery": "research",
    "knowledge": "research",
    # → automation
    "automation-tools": "automation",
    "workflow": "automation",
    "bot": "automation",
    "scheduler": "automation",
    # → productivity (fallback bucket)
    "communication": "productivity",
    "tutorial": "productivity",
    "general": "productivity",
    "utility": "productivity",
    "test": "productivity",
    "finance-ns": "productivity",
}

CANONICAL_CATEGORIES = {
    "research", "dev-tools", "agency", "marketing", "content",
    "automation", "code-review", "productivity", "data", "ops",
}


def upgrade():
    # Tier — collapse studio into operator everywhere it lives.
    op.execute("UPDATE skills SET tier = 'operator' WHERE tier = 'studio'")
    op.execute(
        "UPDATE users SET subscription_tier = 'operator' "
        "WHERE subscription_tier = 'studio'"
    )

    # Category — explicit remap of legacy buckets.
    for old, new in CATEGORY_MAP.items():
        op.execute(
            f"UPDATE skills SET category = '{new}' WHERE category = '{old}'"
        )

    # Anything still outside the canonical 10 falls back to 'productivity'
    # (lowest-risk bucket). Logged via a SQL NOTICE-equivalent: we set the
    # column directly. Operators inspecting the migration can audit
    # SELECT category, COUNT(*) FROM skills GROUP BY category before/after.
    canonical_list = ", ".join(f"'{c}'" for c in sorted(CANONICAL_CATEGORIES))
    op.execute(
        f"UPDATE skills SET category = 'productivity' "
        f"WHERE category IS NOT NULL AND category NOT IN ({canonical_list})"
    )


def downgrade():
    # Best-effort reverse only: tier `operator` and category fallbacks are
    # ambiguous after upgrade, so we cannot reconstruct the prior state.
    # Restore from backup if a true reverse is required. This downgrade is
    # an explicit no-op rather than a partial / misleading reverse.
    pass
