"""atomic-habits 2026-07-20 rank-1 LOOPSKILL — mint v1.0.0 LoopVersion for repo-steward-loop

Revision ID: ah0720_repo_steward_ver
Revises: ah0719_loop_tags
Create Date: 2026-07-20

atomic-habits 2026-07-20 rank-1: GET https://app.loopskill.io/api/loops
(verified_at 2026-07-20T07:0x) shows repo-steward-loop is the ONLY one of 10
loops with latest_version=null — install_count=0 with no version to resolve —
even though it is the flagship (richest description, run_count=1) and its
discovery tags already shipped live via ah0719_loop_tags.

Root cause: prod deploys via .github/workflows/deploy.yml run
`alembic upgrade head` only. They never invoke
scripts/seed_starter_catalog.py's seed_starter_catalog()/_seed_loops() — that
only runs from scripts/bootstrap.py on fresh CONTAINER boot, which prod's
self-hosted-runner systemd deploy path does not use. So repo-steward-loop,
which was published directly against the live DB (see the comment at
scripts/seed_starter_catalog.py:500-508), never received the LoopVersion row
the other 9 starter loops got via _seed_loops()'s "seed a v1.0.0 LoopVersion
if the loop has none" branch.

Fix: mint the same v1.0.0 LoopVersion for repo-steward-loop that
_seed_loops() would produce, by calling the identical manifest-builder
(_loop_manifest_toml) against the canonical STARTER_LOOPS spec — a pure
metadata backfill sourced from the existing SSOT, not a hand-typed manifest.

Idempotent: no-ops if a loop_versions(loop_id, semver='1.0.0') row already
exists, and no-ops if the `loops` row itself doesn't exist in this DB (fresh
CI/test environments that never had repo-steward-loop published directly).
Safe to replay. Downgrade removes only the row this migration inserted.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from typing import Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ah0720_repo_steward_ver"
down_revision: Union[str, Sequence[str], None] = "ah0719_loop_tags"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SLUG = "repo-steward-loop"
_BACKFILL_CHANGELOG = "Initial starter release (backfilled — see ah0719/ah0720 for root cause)."


def _manifest() -> str:
    # Reuse the canonical seed-spec builder so this migration can never drift
    # from scripts/seed_starter_catalog.py's STARTER_LOOPS SSOT.
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from scripts.seed_starter_catalog import STARTER_LOOPS, _loop_manifest_toml

    spec = next(s for s in STARTER_LOOPS if s["slug"] == SLUG)
    return _loop_manifest_toml(spec)


def upgrade() -> None:
    conn = op.get_bind()

    loop_id = conn.execute(sa.text("SELECT id FROM loops WHERE slug = :slug"), {"slug": SLUG}).scalar()
    if loop_id is None:
        # No loops row in this environment — nothing to backfill.
        return

    existing = conn.execute(
        sa.text("SELECT id FROM loop_versions WHERE loop_id = :loop_id AND semver = '1.0.0'"),
        {"loop_id": loop_id},
    ).scalar()
    if existing is not None:
        return

    conn.execute(
        sa.text(
            "INSERT INTO loop_versions (id, loop_id, semver, manifest, changelog) "
            "VALUES (:id, :loop_id, '1.0.0', :manifest, :changelog)"
        ),
        {
            "id": str(uuid4()),
            "loop_id": loop_id,
            "manifest": _manifest(),
            "changelog": _BACKFILL_CHANGELOG,
        },
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM loop_versions "
            "WHERE loop_id = (SELECT id FROM loops WHERE slug = :slug) "
            "AND semver = '1.0.0' AND changelog = :changelog"
        ),
        {"slug": SLUG, "changelog": _BACKFILL_CHANGELOG},
    )
