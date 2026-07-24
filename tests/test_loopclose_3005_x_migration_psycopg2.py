"""Deploy-safety regression for loopclose_3005 Phase X migration
(lc3005_x_cookbook_owner_ck): orphan cleanup + ownership CHECK invariant.

Per alembic-postgres-only-sql-discipline, the SQLite test fixture cannot
enforce a Postgres CHECK constraint and cannot prove the orphan-delete data
step runs before the constraint. This test exercises the migration's real
upgrade() through the same psycopg2 path alembic uses in production, against a
throwaway Postgres database.

Runs only when WITH_POSTGRES=1 (and POSTGRES_DSN points at a reachable admin
connection). Skipped on the default SQLite suite.

Proves:
  1. Pre-existing non-base owner-less orphans are DELETED by the data step.
  2. An is_base=true catalog with NULL owner SURVIVES (legitimately owner-less).
  3. After upgrade, a raw INSERT of a non-base owner-less row is REJECTED by the
     CHECK constraint (the invariant that prevents recurrence).
  4. A non-base row WITH an owner, and an is_base=true row, both insert cleanly.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("WITH_POSTGRES"),
    reason="set WITH_POSTGRES=1 and supply POSTGRES_DSN to run psycopg2 regressions",
)

_MIG_FILE = "lc3005_x_cookbook_owner_ck.py"


def _load_upgrade():
    mig_path = (
        Path(__file__).resolve().parent.parent
        / "alembic" / "versions" / _MIG_FILE
    )
    spec = importlib.util.spec_from_file_location("lc3005_x_migration", mig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_phase_x_migration_cleanup_and_invariant():
    pytest.importorskip("psycopg2")
    import psycopg2
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from psycopg2 import errors as psycopg2_errors

    dsn = os.environ.get(
        "POSTGRES_DSN",
        "postgresql://postgres@127.0.0.1:5499/postgres",
    )
    db_name = f"cbowner_test_{uuid.uuid4().hex[:8]}"

    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db_name};")
    admin.close()

    base_id = str(uuid.uuid4())
    orphan_ids = [str(uuid.uuid4()) for _ in range(3)]
    owned_id = str(uuid.uuid4())
    owner = str(uuid.uuid4())

    engine = None
    pg_conn = None
    try:
        db_dsn = dsn.rsplit("/", 1)[0] + f"/{db_name}"
        engine = sa.create_engine(db_dsn, future=True)

        with engine.begin() as sconn:
            # Minimal cookbooks table mirroring models.Cookbook (the columns the
            # migration touches: is_base, cookbook_owner).
            sconn.execute(sa.text(
                """
                CREATE TABLE cookbooks (
                  id uuid PRIMARY KEY,
                  name text NOT NULL,
                  is_base boolean NOT NULL DEFAULT false,
                  cookbook_owner uuid
                );
                """
            ))
            # Seed prod-shaped state: 1 base catalog (NULL owner, KEEP),
            # 3 non-base orphans (NULL owner, DELETE), 1 owned non-base (KEEP).
            sconn.execute(
                sa.text("INSERT INTO cookbooks (id, name, is_base, cookbook_owner) "
                        "VALUES (:id, 'WiseChef Recipes Catalog', true, NULL)"),
                {"id": base_id},
            )
            for oid in orphan_ids:
                sconn.execute(
                    sa.text("INSERT INTO cookbooks (id, name, is_base, cookbook_owner) "
                            "VALUES (:id, 'MCP Cookbook', false, NULL)"),
                    {"id": oid},
                )
            sconn.execute(
                sa.text("INSERT INTO cookbooks (id, name, is_base, cookbook_owner) "
                        "VALUES (:id, 'My Cookbook', false, :owner)"),
                {"id": owned_id, "owner": owner},
            )

        # Run the migration's real upgrade() bound to a SQLAlchemy Connection,
        # exactly how alembic drives it in production.
        mod = _load_upgrade()
        with engine.begin() as sconn:
            ctx = MigrationContext.configure(sconn)
            with Operations.context(ctx):
                mod.upgrade()

        # 1+2 — orphans gone, base catalog + owned row survive.
        with engine.connect() as sconn:
            rows = sconn.execute(sa.text("SELECT id FROM cookbooks")).fetchall()
        survivor_strs = {str(r[0]) for r in rows}
        assert base_id in survivor_strs, "is_base catalog must survive"
        assert owned_id in survivor_strs, "owned non-base cookbook must survive"
        for oid in orphan_ids:
            assert oid not in survivor_strs, f"orphan {oid} should be deleted"

        # 3 — CHECK rejects a new non-base owner-less insert (raw psycopg2 for
        # clean transaction control).
        pg_conn = psycopg2.connect(db_dsn)
        pg_conn.autocommit = False
        cur = pg_conn.cursor()
        with pytest.raises(psycopg2_errors.CheckViolation):
            cur.execute(
                "INSERT INTO cookbooks (id, name, is_base, cookbook_owner) "
                "VALUES (%s, 'sneaky orphan', false, NULL);",
                (str(uuid.uuid4()),),
            )
            pg_conn.commit()
        pg_conn.rollback()

        # 4 — legitimate rows still insert: a new is_base catalog and an owned one.
        cur.execute(
            "INSERT INTO cookbooks (id, name, is_base, cookbook_owner) "
            "VALUES (%s, 'another base', true, NULL);",
            (str(uuid.uuid4()),),
        )
        cur.execute(
            "INSERT INTO cookbooks (id, name, is_base, cookbook_owner) "
            "VALUES (%s, 'owned', false, %s);",
            (str(uuid.uuid4()), str(uuid.uuid4())),
        )
        pg_conn.commit()
    finally:
        if pg_conn is not None:
            pg_conn.close()
        if engine is not None:
            engine.dispose()
        admin = psycopg2.connect(dsn)
        admin.autocommit = True
        admin.cursor().execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid();",
            (db_name,),
        )
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {db_name};")
        admin.close()
