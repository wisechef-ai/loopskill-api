"""Migration chain verification against real Postgres (the prod dialect).

This test supersedes the SQLite-only ``test_baseline_idempotent.py`` /
``test_upgrade.py`` / ``test_columns_match_model.py`` migration tests, which
cannot validate Postgres-only DDL (FK ALTER, ``information_schema`` reads,
JSONB casts, GIN indexes, ``postgresql.UUID``, ``tsvector``, …).

Why this exists
---------------
Production runs Postgres. Some migrations in this repo use Postgres-only
features (``op.create_foreign_key`` after table creation, ``::jsonb`` casts,
GIN indexes, partial indexes with ``postgresql_where``). The SQLite test
fixture was silently lying — those migrations were never exercised in CI
and the chain-from-baseline test had been broken for weeks until
recipes_2005 surfaced it.

This test runs the same chain against the same dialect production uses,
so what passes here is what will deploy.

How to run
----------
The test is skipped by default to keep the fast SQLite pytest run unchanged.
Two ways to opt in:

1. Local one-shot (recommended dev loop):

       bash scripts/test-migrations-against-postgres.sh

   That script spins ``pgvector/pgvector:pg16`` on port 5499, exports the
   right env vars, runs this test, and tears the container down. ~25s.

2. CI matrix job — set ``WITH_POSTGRES=1`` plus ``POSTGRES_DSN`` to a
   reachable Postgres URL. See ``.github/workflows/ci.yml`` migration-postgres job.

References
----------
- ``alembic-postgres-only-sql-discipline`` skill (templates/test_migration_psycopg2_gate.py)
- tests/migrations/test_baseline_idempotent.py — the SQLite chain test
  this supersedes (kept for the metadata-only stamp invariants it CAN check).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Same constants as the SQLite test — pin the chain points we depend on
BASELINE_REV = "4ba0bf05cd47"

# Tables that the BASELINE production schema had at rev a7f7db696591 (which is
# what BASELINE_DDL captures). Lifted verbatim from
# tests/migrations/test_baseline_idempotent.py.
BASELINE_DDL = """
-- Mirrors the prod baseline schema at rev a7f7db696591. Types match what
-- production actually had (UUID, not TEXT, for id columns) so the FK chain
-- can be enforced. The SQLite fixture in test_baseline_idempotent.py uses
-- TEXT for cross-dialect compat — but Postgres enforces types strictly, so
-- the production-faithful types matter here.
CREATE TABLE IF NOT EXISTS skills (
    id UUID PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    category TEXT,
    readme TEXT,
    license TEXT,
    tier TEXT,
    is_public BOOLEAN DEFAULT true,
    creator_id UUID,
    org_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS telemetry_events (
    id UUID PRIMARY KEY,
    event_type TEXT NOT NULL,
    skill_slug TEXT,
    payload TEXT,
    client_ip TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS install_events (
    id UUID PRIMARY KEY,
    skill_id UUID NOT NULL,
    skill_slug TEXT,
    api_key_id UUID,
    version_semver TEXT,
    client_ip TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);

CREATE TABLE IF NOT EXISTS carousel_entries (
    id UUID PRIMARY KEY,
    skill_id UUID NOT NULL,
    featured_date TIMESTAMP WITH TIME ZONE NOT NULL,
    tagline TEXT,
    position INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now()
);
"""


pytestmark = pytest.mark.skipif(
    not os.environ.get("WITH_POSTGRES"),
    reason=(
        "Set WITH_POSTGRES=1 and POSTGRES_DSN to run the migration chain "
        "against real Postgres. Use scripts/test-migrations-against-postgres.sh "
        "for a one-shot local run."
    ),
)


@pytest.fixture(scope="function")
def fresh_postgres_db():
    """Provision an empty Postgres database, return its DSN. Teardown drops it.

    Function-scoped so each test gets a pristine DB. Running ``alembic
    upgrade head`` is destructive (CREATE TABLE etc.), so sharing state
    across tests means the second test sees columns already added by the
    first — false-positive "DuplicateColumn" errors.
    """
    pytest.importorskip("psycopg2")
    import psycopg2

    admin_dsn = os.environ.get(
        "POSTGRES_DSN",
        "postgresql://postgres:test@127.0.0.1:5499/postgres",
    )
    db_name = f"mig_test_{uuid.uuid4().hex[:8]}"

    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db_name};")
    admin.close()

    db_dsn = admin_dsn.rsplit("/", 1)[0] + f"/{db_name}"
    yield db_dsn

    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    # Force-disconnect any lingering sessions on the test DB before DROP.
    # Tests that open subprocess connections (alembic) can leave sessions
    # alive past the function-scope teardown, which makes DROP DATABASE fail
    # with ObjectInUse.
    admin.cursor().execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid();",
        (db_name,),
    )
    admin.cursor().execute(f"DROP DATABASE IF EXISTS {db_name};")
    admin.close()


def _run_alembic(args: list[str], db_dsn: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WR_DATABASE_URL": db_dsn}
    return subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _seed_baseline_schema(db_dsn: str) -> None:
    """Apply the BASELINE_DDL block as raw SQL via psycopg2.

    Mirrors what BASELINE_DDL captured at rev a7f7db696591 — the tables
    that existed in production before any chain-of-migrations was committed
    to the repo (Skills, telemetry/install events, carousel entries).
    """
    import psycopg2

    conn = psycopg2.connect(db_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(BASELINE_DDL)
    conn.close()


def test_alembic_upgrade_head_from_baseline_postgres(fresh_postgres_db):
    """Full chain runs cleanly from baseline DDL to head on Postgres.

    Regression target: ``b8d2c5a91e3f_subscription`` assumed ``users`` table
    existed (created out-of-band before alembic was wired up); we added
    ``a8b9c0d1e2f3_bootstrap_legacy_tables`` to bridge the gap. This test
    verifies the chain is now self-contained from baseline to head on
    Postgres, the actual production dialect.
    """
    _seed_baseline_schema(fresh_postgres_db)

    stamp = _run_alembic(["stamp", BASELINE_REV], fresh_postgres_db)
    assert stamp.returncode == 0, (
        f"alembic stamp failed:\nSTDOUT: {stamp.stdout}\nSTDERR: {stamp.stderr}"
    )

    upgrade = _run_alembic(["upgrade", "head"], fresh_postgres_db)
    assert upgrade.returncode == 0, (
        f"alembic upgrade head failed:\nSTDOUT: {upgrade.stdout}\nSTDERR: {upgrade.stderr}"
    )

    # Verify we actually arrived at HEAD (don't trust returncode alone).
    current = _run_alembic(["current"], fresh_postgres_db)
    assert current.returncode == 0
    # Head changes over time, but alembic-current always prints the rev.
    # Check it contains "(head)" — alembic appends that marker.
    assert "(head)" in current.stdout, (
        f"upgrade succeeded but current revision is not head: {current.stdout}"
    )


def test_bootstrap_legacy_tables_creates_users_with_baseline_columns(fresh_postgres_db):
    """``a8b9c0d1e2f3`` creates the 5 legacy tables with baseline-revision columns.

    Verifies what we promised in the bootstrap migration's docstring:
      - users, api_keys, creators, creator_payouts, referrals all exist
      - users has the BASELINE columns (id, github_id, ..., NO subscription_*)
        — subscription_* are added by the next migration (b8d2c5a91e3f).
    """
    import psycopg2

    _seed_baseline_schema(fresh_postgres_db)
    stamp = _run_alembic(["stamp", BASELINE_REV], fresh_postgres_db)
    assert stamp.returncode == 0

    upgrade_to_bootstrap = _run_alembic(
        ["upgrade", "a8b9c0d1e2f3"], fresh_postgres_db
    )
    assert upgrade_to_bootstrap.returncode == 0, (
        f"alembic upgrade to bootstrap failed:\n"
        f"STDOUT: {upgrade_to_bootstrap.stdout}\nSTDERR: {upgrade_to_bootstrap.stderr}"
    )

    conn = psycopg2.connect(fresh_postgres_db)
    cur = conn.cursor()

    for table in ("users", "api_keys", "creators", "creator_payouts", "referrals"):
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name = %s;",
            (table,),
        )
        assert cur.fetchone(), f"bootstrap migration did not create '{table}'"

    # Sanity: baseline `users` MUST NOT have subscription columns yet.
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='users';"
    )
    user_cols = {r[0] for r in cur.fetchall()}
    assert "id" in user_cols
    assert "github_id" in user_cols
    assert "email" in user_cols
    # NOT YET — these get added by b8d2c5a91e3f after this revision.
    for not_yet in (
        "stripe_customer_id",
        "subscription_status",
        "subscription_tier",
        "referral_code",
        "referred_by",
        "discord_user_id",
        "utm_ref",
    ):
        assert not_yet not in user_cols, (
            f"baseline 'users' shouldn't have '{not_yet}' yet — that's "
            f"added by a later migration, not this bootstrap."
        )

    conn.close()


def test_model_columns_present_after_full_upgrade(fresh_postgres_db):
    """After ``upgrade head``, every column declared in ``app.models`` exists.

    Detects model-vs-migration drift — if someone adds a column to a model
    and forgets the migration (or vice versa), this catches it on Postgres.
    """
    import psycopg2
    from sqlalchemy import inspect as sa_inspect

    _seed_baseline_schema(fresh_postgres_db)
    assert _run_alembic(["stamp", BASELINE_REV], fresh_postgres_db).returncode == 0
    upgrade = _run_alembic(["upgrade", "head"], fresh_postgres_db)
    assert upgrade.returncode == 0, upgrade.stderr

    # Compare model columns to live schema for the five bootstrapped tables.
    from app.models import APIKey, Creator, CreatorPayout, Referral, User

    conn = psycopg2.connect(fresh_postgres_db)
    cur = conn.cursor()

    for model in (User, APIKey, Creator, CreatorPayout, Referral):
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name = %s;",
            (model.__tablename__,),
        )
        live_cols = {r[0] for r in cur.fetchall()}
        model_cols = {col.name for col in sa_inspect(model).columns}
        missing = model_cols - live_cols
        # Filter out cols that the migration chain genuinely doesn't manage
        # (e.g. server-side compute columns). For now we expect zero missing.
        assert not missing, (
            f"{model.__tablename__}: model declares {missing} but migration "
            f"chain doesn't create them. Either add a migration or drop "
            f"the column from the model."
        )

    conn.close()


def test_tier_drift_sweep_archived_renames_archived_legacy_rows(fresh_postgres_db):
    """``h2i3j4k5l6m7`` renames archived legacy-tier rows that g1h2i3j4k5l6 skipped.

    Regression target: the original Phase G sweep (g1h2i3j4k5l6) only touched
    non-archived rows. That left archived rows on legacy slugs
    ('cook'/'operator'/'studio') which become orphaned to the canonical tier
    vocabulary once the 30-day READ-alias window closes on 2026-06-10.

    This test:
      1. Upgrades to ``g1h2i3j4k5l6`` (the non-archived sweep).
      2. Seeds archived rows with the legacy slugs.
      3. Upgrades to ``h2i3j4k5l6m7`` (the archived sweep).
      4. Asserts: zero rows with legacy slugs anywhere in the table.
    """
    import psycopg2

    _seed_baseline_schema(fresh_postgres_db)
    assert _run_alembic(["stamp", BASELINE_REV], fresh_postgres_db).returncode == 0

    # Migrate up to (but not past) the original non-archived sweep.
    upgrade_to_g = _run_alembic(["upgrade", "g1h2i3j4k5l6"], fresh_postgres_db)
    assert upgrade_to_g.returncode == 0, (
        f"upgrade to g1h2i3j4k5l6 failed:\nSTDOUT: {upgrade_to_g.stdout}\n"
        f"STDERR: {upgrade_to_g.stderr}"
    )

    # Seed: archived rows with legacy slugs (the prod state on 2026-05-20).
    # The catalog has a NOT-NULL trigger
    # `catalog_no_phantom_public_skill` that rejects public+non-archived
    # skills without a published version, so seed everything is_public=false
    # to avoid tripping it; tier-slug logic is independent of is_public.
    conn = psycopg2.connect(fresh_postgres_db)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO skills (id, slug, title, tier, is_archived, is_public)
        VALUES
          (gen_random_uuid(), 'legacy-cook-1', 'Legacy cook 1', 'cook',     true,  false),
          (gen_random_uuid(), 'legacy-cook-2', 'Legacy cook 2', 'cook',     true,  false),
          (gen_random_uuid(), 'legacy-op-1',   'Legacy op 1',   'operator', true,  false),
          (gen_random_uuid(), 'legacy-studio', 'Legacy studio', 'studio',   true,  false),
          (gen_random_uuid(), 'live-pro',      'Live pro',      'pro',      false, false),
          (gen_random_uuid(), 'live-free',     'Live free',     'free',     false, false);
        """
    )

    # Sanity: pre-sweep, legacy rows are there.
    cur.execute(
        "SELECT tier, COUNT(*) FROM skills "
        "WHERE tier IN ('cook','operator','studio') AND is_archived=true "
        "GROUP BY tier ORDER BY tier;"
    )
    pre = dict(cur.fetchall())
    assert pre == {"cook": 2, "operator": 1, "studio": 1}, (
        f"seed didn't land as expected: {pre}"
    )

    # Apply the archived sweep.
    upgrade_to_h = _run_alembic(["upgrade", "h2i3j4k5l6m7"], fresh_postgres_db)
    assert upgrade_to_h.returncode == 0, (
        f"upgrade to h2i3j4k5l6m7 failed:\nSTDOUT: {upgrade_to_h.stdout}\n"
        f"STDERR: {upgrade_to_h.stderr}"
    )

    # Post-sweep: ZERO rows with any legacy slug, anywhere.
    cur.execute(
        "SELECT COUNT(*) FROM skills "
        "WHERE tier IN ('cook','operator','studio');"
    )
    remaining = cur.fetchone()[0]
    assert remaining == 0, (
        f"after h2i3j4k5l6m7, {remaining} rows still have legacy tier slugs. "
        f"Archived sweep did not complete the migration."
    )

    # Counts moved to the right canonical buckets.
    cur.execute(
        "SELECT tier, COUNT(*) FROM skills WHERE is_archived=true "
        "GROUP BY tier ORDER BY tier;"
    )
    arch = dict(cur.fetchall())
    assert arch.get("pro") == 2, f"archived cook→pro count wrong: {arch}"
    assert arch.get("pro_plus") == 2, (
        f"archived operator+studio→pro_plus count wrong: {arch}"
    )

    conn.close()
