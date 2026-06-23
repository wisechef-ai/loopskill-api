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

_skills = sa.table(
    "skills",
    sa.column("id", sa.String()),
    sa.column("slug", sa.String()),
    sa.column("is_public", sa.Boolean()),
    sa.column("is_archived", sa.Boolean()),
    sa.column("archived_at", sa.DateTime()),
    sa.column("search_vector", sa.Text()),
)

_skill_versions = sa.table(
    "skill_versions",
    sa.column("skill_id", sa.String()),
    sa.column("id", sa.String()),
)


def upgrade() -> None:
    """Archive any public, non-archived skill row with no published versions."""
    bind = op.get_bind()

    # Find all phantom rows: public + not archived + 0 versions.
    # Cast-free SQL so this runs on both Postgres and SQLite.
    result = bind.execute(
        sa.text(
            """
            SELECT s.id AS id, s.slug
            FROM skills s
            LEFT JOIN skill_versions v ON v.skill_id = s.id
            WHERE s.is_public = TRUE
              AND s.is_archived = FALSE
            GROUP BY s.id, s.slug
            HAVING COUNT(v.id) = 0
            """
        )
    )
    phantom_rows = [(str(row[0]), row[1]) for row in result.fetchall()]

    if not phantom_rows:
        return

    # Archive them — use dialect-aware approach to avoid Postgres-specific
    # array syntax (ANY(CAST(:ids AS uuid[]))) that SQLite can't parse.
    ids = [pid for pid, _slug in phantom_rows]
    is_pg = bind.dialect.name == "postgresql"

    if is_pg and len(ids) > 1:
        # Postgres: efficient single UPDATE with ANY
        bind.execute(
            sa.text(
                """
                UPDATE skills
                SET is_archived = TRUE,
                    archived_at = COALESCE(archived_at, NOW()),
                    search_vector = NULL
                WHERE id::text = ANY(:ids)
                """
            ).bindparams(sa.bindparam("ids", ids)),
        )
    else:
        # SQLite (or single-row fast path): row-by-row UPDATE
        for skill_id in ids:
            bind.execute(
                sa.text(
                    "UPDATE skills SET is_archived = TRUE, "
                    "archived_at = COALESCE(archived_at, CURRENT_TIMESTAMP), "
                    "search_vector = NULL WHERE id = :id"
                ).bindparams(id=skill_id)
            )

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
