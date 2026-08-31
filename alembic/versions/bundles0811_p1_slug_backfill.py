"""bundles0811-P1 (F5) — backfill NULL slug on the live public bundle row.

Revision ID: bundles0811_p1_slug_backfill
Revises: 06671beefcd1
Create Date: 2026-08-11

The cold-path trace (2026-08-11, projects/loopskill/
2026-08-11-bundles0811-p1-coldpath-trace.md, friction F5) found ONE live
public bundle with ``slug IS NULL``: ``agent_marketing`` (25 skills,
visibility='public'). Every public route (``GET /api/bundles/public/{slug}``,
``GET /bundles/p?slug=``, the discover feed, the well-known bridge) is
slug-addressed, so this bundle is public in name and unreachable in
practice — it has no shareable URL.

This is a pure DATA backfill — one row, mirroring
``bundle_routes._ensure_bundle_slug``'s slugify + collision-suffix discipline
exactly (same regex, same suffix loop) so the value this migration writes is
byte-identical to what the app would compute for the same name. No schema
change; the ``bundles.slug`` column and its unique index already exist.

Idempotent: re-running only touches rows that are STILL
``visibility='public' AND slug IS NULL`` — a second run is a no-op once the
first has landed. Companion enforcement (Bundle._require_slug_when_public,
app/models.py, same PR) makes this class of row structurally impossible to
create going forward; this migration only cleans up the one row that
predates that guard.

DOWNGRADE: documented no-op. Reconstructing "the slug was NULL" is
meaningless — the bundle was already public with this name; restoring NULL
would only re-break its URL. Same precedent as this repo's other lossy-
backfill migrations (see AGENTS.md / skill notes on migration downgrades).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bundles0811_p1_slug_backfill"
down_revision: Union[str, Sequence[str], None] = "06671beefcd1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _slugify(name: str) -> str:
    """Byte-identical copy of app.bundle_deployment_routes._slugify.

    Migrations must not import app.* route modules for logic that runs
    against a live table (the app module graph can change shape under a
    migration in ways the DB schema at this revision doesn't reflect) —
    copied deliberately, not imported. Keep in sync if the app's slugify
    rule ever changes.
    """
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:64] or "bundle"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, name FROM bundles WHERE visibility = 'public' AND slug IS NULL"
        )
    ).fetchall()
    for row_id, name in rows:
        base_slug = _slugify(name)
        slug = base_slug
        suffix = 0
        while (
            bind.execute(
                sa.text("SELECT 1 FROM bundles WHERE slug = :slug AND id != :id"),
                {"slug": slug, "id": row_id},
            ).first()
            is not None
        ):
            suffix += 1
            slug = f"{base_slug}-{suffix}"
        bind.execute(
            sa.text("UPDATE bundles SET slug = :slug WHERE id = :id"),
            {"slug": slug, "id": row_id},
        )


def downgrade() -> None:
    """No-op — see module docstring. Restoring NULL would re-break the URL."""
