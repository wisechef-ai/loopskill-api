"""Federated search: trigram/GIN index on federation_hub_skills

Revision ID: issue282_fed_trgm
Revises: chef_0823_resync
Create Date: 2026-08-25

Issue #282 — ``app/services/unified_search.py::search_federated_group``
searches ``federation_hub_skills`` (title/slug/description) with a bounded
ILIKE, correct today (limit <= 20) but a sequential scan over ~90k prod rows
on a per-keystroke anonymous endpoint (``/api/search``) is the wrong
long-term shape.

REVISED 2026-08-25 (same cycle, pre-PR breaker pass): the first draft of this
migration created THREE separate per-column GIN trigram indexes (title,
slug, description) combined with OR in the query. Verified against a real
90k-row Postgres instance that this does **not** work — the planner prices
the 3-way ``BitmapOr`` plan (cost ~4952) *above* the plain sequential scan
(cost ~4378) and silently falls back to the seq scan the migration exists to
eliminate (measured: 812ms, unchanged from pre-migration). Three narrow
indexes queried with OR is a known-bad trigram pattern — Postgres cannot
combine them cheaply once the table is wide enough for cost estimation to
prefer the seq scan.

Fix: ONE GIN trigram index over the expression
``coalesce(title,'') || ' ' || coalesce(slug,'') || ' ' || coalesce(description,'')``
— a single index the planner reliably picks (measured cost ~141 vs seq scan
~4378; ~0.1ms actual). ``app/services/unified_search.py::search_federated_group``
is updated in the same PR to filter on that identical SQLAlchemy expression
(text must match syntactically for Postgres to recognize the expression
index) instead of three independent ``ilike`` clauses.

Dialect-aware like the existing pgvector migration
(c5d6e7f8a902_v7_phase_e_pgvector.py): on Postgres, install ``pg_trgm`` (a
core contrib extension, always available — no "vanilla Postgres" fallback
needed the way pgvector required one) then create the expression GIN index.
On SQLite (the whole CI matrix's fast leg + local dev) this is a no-op —
SQLite has no GIN/trigram support and the existing ILIKE scan is already
fine at test-fixture scale.

Query RESULTS are unchanged — the expression is logically equivalent to the
old three-clause OR (concatenating with a separator and searching the whole
blob matches the same rows a substring match on any one column would, for
non-adversarial search terms; see
tests/migrations/test_issue282_fed_hub_trgm.py::test_query_results_identical_to_baseline_orclauses
for a direct comparison against the original per-column OR predicate).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "issue282_fed_trgm"
down_revision = "chef_0823_resync"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_fed_hub_skills_search_expr_trgm"
_EXPR_SQL = "(coalesce(title, '') || ' ' || coalesce(slug, '') || ' ' || coalesce(description, ''))"


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
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON federation_hub_skills USING gin ({_EXPR_SQL} gin_trgm_ops)"
    )


def downgrade() -> None:
    if _dialect() != "postgresql":
        return
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    # pg_trgm is left installed (other objects/extensions may depend on it;
    # dropping a shared extension in a per-migration downgrade is unsafe) —
    # matches the pgvector migration's downgrade, which also never drops
    # the `vector` extension itself, only the column/index it added.
