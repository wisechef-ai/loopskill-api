"""atomic-habits 2026-07-21 rank-1 (+ rank-8 REVENUE/CATALOG) — mint v1.0.0
CompositeLoopVersion for atomic-habits and dreaming.

Revision ID: ah0721_composite_loop_ver
Revises: ah0720_repo_steward_ver
Create Date: 2026-07-21

atomic-habits 2026-07-21 rank-1: GET https://app.loopskill.io/api/composite-loops
(verified_at 2026-07-21T07:04) shows the FLAGSHIP composite loop "atomic-habits"
(the headline loop the whole engine is named after) with latest_version=null,
install_count=0, is_public=true, tier=free — uninstallable via the run->install
bridge (#31fb341) and fleet-deploy endpoint (#125) that shipped this week.

atomic-habits 2026-07-21 rank-8 REVENUE/CATALOG: the same GET shows the
"dreaming" composite loop in the identical state — the second uninstallable
public loop, no stale-block (no repeated blocked rank-8 title in the last 3
executed/<date>.json).

Root cause (same class as ah0720/repo-steward-loop, but for the NEW
composite_loops surface instead of the old `loops` table): both rows were
published directly against the live app.loopskill.io DB — there is no
STARTER_LOOPS-equivalent SSOT for composite_loops in this repo, no seed
script backfills composite_loop_versions, and prod deploys only run
`alembic upgrade head` (deploy.yml), never a fresh-container bootstrap path.
So neither loop ever received the v1.0.0 CompositeLoopVersion row that
POST /api/composite-loops/{slug}/versions would normally mint.

Fix: mint the v1.0.0 CompositeLoopVersion for each loop, with `manifest`
built in the exact shape publish_composite_loop_version() would produce
(see app/composite_loop_routes.py:201-213), sourced verbatim from the live
GET /api/composite-loops/{slug} detail response (verified via curl
2026-07-21T07:0x) — a pure metadata backfill, not a behavior change.

Idempotent: no-ops per-loop if a composite_loop_versions(composite_loop_id,
semver='1.0.0') row already exists, and no-ops if the `composite_loops` row
itself doesn't exist in this DB (fresh CI/test environments that never had
these loops published directly). Safe to replay. Downgrade removes only the
rows this migration inserted (matched by changelog marker).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ah0721_composite_loop_ver"
down_revision: Union[str, Sequence[str], None] = "ah0720_repo_steward_ver"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BACKFILL_CHANGELOG = "Initial starter release (backfilled — see ah0721 for root cause)."

# Verbatim from live GET /api/composite-loops/{slug} detail response
# (verified_at 2026-07-21T07:0x). Manifest shape mirrors
# app/composite_loop_routes.py:publish_composite_loop_version's `manifest` dict.
_BACKFILL_SPECS: list[dict] = [
    {
        "slug": "atomic-habits",
        "title": "Atomic Habits \u2014 1% daily improvement loop",
        "schedule": "24h",
        "skills": [{"slug": "skill-creator"}, {"slug": "writing-skills"}],
        "connectors": [],
        "subagents_config": {
            "maker": {
                "model_tier": "sonnet",
                "toolsets": ["terminal", "read_file", "write_file", "patch", "search_files"],
            },
            "checker": {"model_tier": "haiku", "toolsets": ["read_file", "search_files"]},
        },
        "verifier_slug": "test-green-loop",
        "state_seed": {"last_improvement": None, "streak_count": 0},
        "budget_usd": 2.0,
        "prompt": (
            "Review the agent's recent work from today. Identify exactly ONE small "
            "(1%) improvement: a missing skill step, a documentation gap, a stale "
            "fixture, or a minor code quality issue. Ship the fix. Verify it landed."
        ),
        "residency": None,
    },
    {
        "slug": "dreaming",
        "title": "Dreaming \u2014 nightly memory consolidation",
        "schedule": "24h",
        "skills": [{"slug": "memory-dreaming"}],
        "connectors": [],
        "subagents_config": {
            "maker": {"model_tier": "haiku", "toolsets": ["terminal", "read_file", "search_files"]},
            "checker": {"model_tier": "haiku", "toolsets": ["read_file"]},
        },
        "verifier_slug": "test-green-loop",
        "state_seed": {"last_dream_at": None, "learnings_extracted": 0, "memories_pruned": 0},
        "budget_usd": 1.0,
        "prompt": (
            "Consolidate the last 24 hours of conversation history. Extract new "
            "learnings and store them. Apply decay scoring to older memories. Flag "
            "any that fall below the removal threshold. Produce a summary of what "
            "was consolidated."
        ),
        "residency": None,
    },
]


def _manifest_json(spec: dict) -> str:
    manifest = {
        "slug": spec["slug"],
        "title": spec["title"],
        "schedule": spec["schedule"],
        "skills": spec["skills"],
        "connectors": spec["connectors"],
        "subagents_config": spec["subagents_config"],
        "verifier_slug": spec["verifier_slug"],
        "state_seed": spec["state_seed"],
        "budget_usd": spec["budget_usd"],
        "prompt": spec["prompt"],
        "residency": spec["residency"],
    }
    return json.dumps(manifest)


def upgrade() -> None:
    conn = op.get_bind()

    for spec in _BACKFILL_SPECS:
        slug = spec["slug"]
        loop_id = conn.execute(
            sa.text("SELECT id FROM composite_loops WHERE slug = :slug"), {"slug": slug}
        ).scalar()
        if loop_id is None:
            # No composite_loops row in this environment — nothing to backfill.
            continue

        existing = conn.execute(
            sa.text(
                "SELECT id FROM composite_loop_versions "
                "WHERE composite_loop_id = :loop_id AND semver = '1.0.0'"
            ),
            {"loop_id": loop_id},
        ).scalar()
        if existing is not None:
            continue

        conn.execute(
            sa.text(
                "INSERT INTO composite_loop_versions "
                "(id, composite_loop_id, semver, manifest, changelog) "
                "VALUES (:id, :loop_id, '1.0.0', :manifest, :changelog)"
            ),
            {
                "id": str(uuid4()),
                "loop_id": loop_id,
                "manifest": _manifest_json(spec),
                "changelog": _BACKFILL_CHANGELOG,
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    for spec in _BACKFILL_SPECS:
        conn.execute(
            sa.text(
                "DELETE FROM composite_loop_versions "
                "WHERE composite_loop_id = (SELECT id FROM composite_loops WHERE slug = :slug) "
                "AND semver = '1.0.0' AND changelog = :changelog"
            ),
            {"slug": spec["slug"], "changelog": _BACKFILL_CHANGELOG},
        )
