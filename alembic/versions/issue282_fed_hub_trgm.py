"""Federated search: trigram/GIN index on federation_hub_skills

Revision ID: issue282_fed_trgm
Revises: chef_0823_resync
Create Date: 2026-08-25

Issue #282 — ``app/services/unified_search.py::search_federated_group``
searches ``federation_hub_skills`` (title/slug/description) with a bounded
ILIKE, correct today (limit <= 20) but a sequential scan over ~90k prod rows
on a per-keystroke anonymous endpoint (``/api/search``) is the wrong
long-term shape.

This migration adds ``pg_trgm`` GIN indexes on the three ILIKE'd columns —
``title``, ``slug``, ``description`` — so Postgres can satisfy
``column ILIKE '%term%'`` with an index scan instead of a seq scan
(``gin_trgm_ops`` supports arbitrary-substring LIKE/ILIKE, unlike a plain
btree which only helps prefix matches).

Dialect-aware like the existing pgvector migration
(c5d6e7f8a902_v7_phase_e_pgvector.py): on Postgres, install ``pg_trgm`` (a
core contrib extension, always available — no "vanilla Postgres" fallback
needed the way pgvector required one) then create three GIN indexes. On
SQLite (the whole CI matrix's fast leg + local dev) this is a no-op — SQLite
has no GIN/trigram support and the existing ILIKE scan is already fine at
test-fixture scale.

No application code changes in this migration — ``search_federated_group``'s
ILIKE queries are byte-for-byte unchanged; only their execution plan changes.
Query results are therefore guaranteed identical (see
tests/test_277_federated_reachability.py + test_issue282_trgm_index.py: same
assertions before and after).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "issue282_fed_trgm"
down_revision = "chef_0823_resync"
branch_labels = None
depends_on = None

_INDEXES = {
    "ix_fed_hub_skills_title_trgm": "title",
    "ix_fed_hub_skills_slug_trgm": "slug",
    "ix_fed_hub_skills_description_trgm": "description",
}


def _dialect() -> str:
    return op.get_bind().dialect.name


def upgrade() -> None:
    if _dialect() != "postgresql":
        # SQLite (test suite, local dev bootstrap): no GIN/trigram support,
        # and the ILIKE scan is fine at fixture scale. No-op, matching the
        # pgvector migration's cross-dialect posture.
        return

    # pg_trgm ships in the postgres-contrib package present on every managed
    # Postgres (RDS, Cloud SQL, postgres:17 image) and needs no availability
    # probe the way pgvector did (pgvector is a third-party extension that
    # may genuinely be absent; pg_trgm is core contrib, always installable).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for index_name, column in _INDEXES.items():
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} "
            f"ON federation_hub_skills USING gin ({column} gin_trgm_ops)"
        )


def downgrade() -> None:
    if _dialect() != "postgresql":
        return
    for index_name in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {index_name}")
    # pg_trgm is left installed (other objects/extensions may depend on it;
    # dropping a shared extension in a per-migration downgrade is unsafe) —
    # matches the pgvector migration's downgrade, which also never drops
    # the `vector` extension itself, only the column/index it added.
