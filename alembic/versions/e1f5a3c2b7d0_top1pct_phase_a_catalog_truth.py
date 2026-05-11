"""top1pct_1105 Phase A — catalog truth pass.

Soft-archives 7 dead-weight / leaky skills, scrubs internal-name leaks, and
back-fills tier=cook for 2 orphan rows. Also merges 7 alembic heads.

Soft-delete via `is_archived=true` — schema column already exists from
b2f4d8e9a1c3 (v7.1 Phase 4). `/api/skills/search` filters out is_archived
skills so they vanish from the catalog while install-event history is
preserved (F2 in plan premortem).

Kill list (from plan §0):
  stable-diffusion, grok-search, xitter, nano-banana-pro, elevenlabs-pro,
  minto, code-reviewer

Leaky description scrubs:
  super-memory                 → strip "Tori memory stack"
  stripe-live-price-rotation   → strip "Scan free / Rescue $7 / Ward $29/mo"
  image-generator              → expand 50-char generic blurb

Orphan tier back-fill:
  agent-rescue, launch-readiness-check (both tier=NULL) → tier=cook
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e1f5a3c2b7d0"
down_revision: Union[str, Sequence[str], None] = (
    "c7a8b9d0e1f2",  # v7 phase J skill aliases
    "b2c3d4e5f6a1",  # skill patches
    "c5d6e7f8a902",  # v7 phase E pgvector
    "a3f1e9b5c7d2",  # v7.1 share tokens
    "b2f4d8e9a1c3",  # v7.1 p4 search_vector + archive
    "d7a3c1f9e201",  # intent_survey_responses
    "a1c2d3e4f5g6",  # creator payout referral support
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


KILL_LIST = (
    "stable-diffusion",
    "grok-search",
    "xitter",
    "nano-banana-pro",
    "elevenlabs-pro",
    "minto",
    "code-reviewer",
)

# Replacement descriptions — short, factual, leak-free. Rewritten descriptions
# come from the original SKILL.md `description:` field with internal-name
# placeholders generalised. Kept ≤300 chars so search snippets stay clean.
SCRUBBED_DESCRIPTIONS = {
    "super-memory": (
        "One-command installer for a full agent-memory stack: cognee 1.0.x "
        "knowledge graph on pghybrid, LiteLLM proxy with rotation, watchdog "
        "cron, nightly ingest, CouchDB single-node document store, and a "
        "markdown vault wired to Obsidian. Numbered scripts run in order; the "
        "top-level orchestrator runs only what is missing. Use when "
        "provisioning a fresh Linux box for an agent that needs persistent "
        "cross-session recall."
    ),
    "stripe-live-price-rotation": (
        "Rotate live Stripe prices on a running production product without "
        "breaking checkout for in-flight customers. Creates a new Price, "
        "swaps it on the Product as default, deactivates the old Price. Works "
        "with existing subscriptions (they keep their old price unless you "
        "explicitly migrate)."
    ),
    "image-generator": (
        "Generate images from text prompts using Stable Diffusion, FLUX, or "
        "OpenAI's gpt-image-1. Picks the right backend based on cost, style, "
        "and resolution constraints. Returns a URL or local file path."
    ),
}


def upgrade() -> None:
    skills = sa.table(
        "skills",
        sa.column("slug", sa.String),
        sa.column("tier", sa.String),
        sa.column("description", sa.Text),
        sa.column("is_archived", sa.Boolean),
        sa.column("is_public", sa.Boolean),
    )

    # 1. Soft-archive 7 kill-list skills
    op.execute(
        skills.update()
        .where(skills.c.slug.in_(KILL_LIST))
        .values(is_archived=True, is_public=False)
    )

    # 2. Scrub leaky descriptions
    for slug, new_desc in SCRUBBED_DESCRIPTIONS.items():
        op.execute(skills.update().where(skills.c.slug == slug).values(description=new_desc))

    # 3. Back-fill orphan tier rows (agent-rescue, launch-readiness-check are
    #    real skills with tier=NULL; treat as Pro since they're not free)
    op.execute(
        skills.update()
        .where(skills.c.tier.is_(None))
        .where(skills.c.is_archived == False)  # noqa: E712
        .values(tier="cook")
    )


def downgrade() -> None:
    """Best-effort reversal — descriptions cannot be perfectly restored.

    Restore is_public=True + is_archived=False for the 7 kill-list skills.
    Tier back-fill (NULL→cook) is NOT reversed because the original NULL state
    was a schema bug, not a feature.
    """
    skills = sa.table(
        "skills",
        sa.column("slug", sa.String),
        sa.column("is_archived", sa.Boolean),
        sa.column("is_public", sa.Boolean),
    )
    op.execute(
        skills.update()
        .where(skills.c.slug.in_(KILL_LIST))
        .values(is_archived=False, is_public=True)
    )
