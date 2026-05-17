"""Catalog hygiene — archive phantom rows missing latest_version

Issue #109: ``local-skills-discovery`` (and any other row in the same state)
is listed in the public catalog with ``is_public=true``, ``is_archived=false``,
and a non-trivial ``quality_score`` — but has ZERO published versions. Calling
``recipes_install`` against it returns ``{"error":"no_versions"}``. This is
exactly the "catalog reality lie" pattern polish_1805 set out to kill.

Two changes:

1. Archive every row matching ``is_public=true AND is_archived=false AND
   no rows in skill_versions for skill_id``. Stamps ``archived_at`` to now()
   so the audit trail is intact.

2. Drop the BM25 ``search_vector`` for those rows so they stop appearing in
   keyword search until a real version is published.

The CHECK constraint preventing the state from re-occurring lives in a
separate migration (see ``catalog_invariant_no_phantom_public_rows``) — that
constraint is partial-index-based and Postgres-only, while this migration is
the one-shot cleanup that should run on every environment.

Revision ID: f00d1109cafe
Revises: fb89c02e7332
Create Date: 2026-05-17 21:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f00d1109cafe"
down_revision: Union[str, None] = "fb89c02e7332"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Archive any public, non-archived skill row with no published versions."""
    bind = op.get_bind()

    # Find all phantom rows: public + not archived + 0 versions.
    result = bind.execute(
        sa.text(
            """
            SELECT s.id::text AS id, s.slug
            FROM skills s
            LEFT JOIN skill_versions v ON v.skill_id = s.id
            WHERE s.is_public = TRUE
              AND s.is_archived = FALSE
            GROUP BY s.id, s.slug
            HAVING COUNT(v.id) = 0
            """
        )
    )
    phantom_rows = [(row[0], row[1]) for row in result.fetchall()]

    if not phantom_rows:
        return

    # Archive them in one statement, stamping archived_at.
    ids = [pid for pid, _slug in phantom_rows]
    bind.execute(
        sa.text(
            """
            UPDATE skills
            SET is_archived = TRUE,
                archived_at = COALESCE(archived_at, NOW()),
                search_vector = NULL
            WHERE id = ANY(CAST(:ids AS uuid[]))
            """
        ).bindparams(sa.bindparam("ids", ids, type_=sa.ARRAY(sa.String()))),
    )

    # Log the cleanup so the migration leaves a paper trail for ops review.
    slugs = ", ".join(slug for _id, slug in phantom_rows)
    print(
        f"[migration f00d1109cafe] archived {len(phantom_rows)} phantom "
        f"public skill rows with no versions: {slugs}"
    )


def downgrade() -> None:
    """Un-archive cannot be done blindly — we don't know which rows were
    archived by THIS migration vs. by hand. Downgrade is a no-op; operators
    must un-archive manually via the admin route if needed.
    """
    pass
