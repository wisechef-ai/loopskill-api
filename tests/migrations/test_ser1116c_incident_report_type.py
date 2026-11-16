"""Test for ser1116c — incident_reports.report_type add + provenance backfill.

Runs the migration against a minimal SQLite schema holding real rows of every
shape, so the backfill's classification is EXERCISED rather than asserted from
reading the code. The same statements are also run against Postgres in CI
(tests/migrations/test_chain_postgres.py runs the whole chain and then checks
that every model column exists), and it was a Postgres run that caught the
original bug in this pattern: ``sa.update(tbl)`` can only set columns declared on
the lightweight ``sa.table()`` it was handed, so omitting ``report_type`` raised
``CompileError: Unconsumed column names`` at deploy time — invisible to any
model-level test.

The seed deliberately covers both directions the heuristic can be wrong in
(documented in the migration docstring), not just the shapes production
happens to contain.
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

_VERSIONS_DIR = Path(__file__).parents[2] / "alembic" / "versions"
if str(_VERSIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_VERSIONS_DIR))

MOD_NAME = "ser1116c_incident_report_type"

# (id, env_fingerprint raw, command, exit_code, expected report_type)
_SEED = [
    # The shape every real prod row has (11/11 on 2026-11-16): a skill error.
    ("r1", "{}", None, None, "skill_error"),
    # A crash report: fingerprint populated, command + exit code present.
    ("r2", '{"os": "linux"}', "run --x", 1, "incident"),
    ("r3", "{}", "run --x", 1, "incident"),
    ("r4", "{}", None, None, "skill_error"),
    # A non-dict JSON fingerprint carries information -> not "empty" -> crash.
    ("r5", "[]", None, None, "incident"),
    ("r6", '{"a": 1}', "run", 0, "incident"),
    # The heuristic's honest failure modes, pinned as expectations rather than
    # buried in prose: a skill error POSTed WITH a command reads as a crash
    # (under-count), and a crash sent with only a signature reads as a skill
    # error (over-count). Both are historical-tail-only; writers classify now.
    ("r7-skill-with-cmd", "{}", "loopskill run foo", 2, "incident"),
    ("r8-crash-bare", "{}", None, None, "skill_error"),
]


def _engine_with_table():
    """In-memory SQLite pre-migration: incident_reports WITHOUT report_type."""
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE incident_reports ("
                " id TEXT PRIMARY KEY,"
                " error_signature TEXT NOT NULL,"
                " env_fingerprint TEXT,"
                " agent_fp_anon TEXT,"
                " occurred_at TEXT,"
                " command TEXT,"
                " exit_code INTEGER,"
                " stack_trace_top TEXT)"
            )
        )
        for rid, fp, cmd, ec, _exp in _SEED:
            conn.execute(
                text(
                    "INSERT INTO incident_reports"
                    " (id, error_signature, env_fingerprint, agent_fp_anon,"
                    "  occurred_at, command, exit_code, stack_trace_top)"
                    " VALUES (:id, :sig, :fp, 'agent', '2026-11-16', :cmd, :ec, 'trace')"
                ),
                {"id": rid, "sig": f"sig-{rid}", "fp": fp, "cmd": cmd, "ec": ec},
            )
        conn.commit()
    return engine


class _MigrationRun:
    """Bind an alembic Operations context to one throwaway engine."""

    def __init__(self, engine) -> None:
        self.engine = engine

    def __call__(self, fn) -> None:
        with self.engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                fn()


@pytest.fixture()
def mig():
    return importlib.import_module(MOD_NAME)


def test_migration_metadata(mig) -> None:
    assert mig.revision == "ser1116c_incident_report_type"
    # Pinned to the single head at authoring. If this assertion fails because
    # another migration merged first, RE-PIN — do not add a second head.
    assert mig.down_revision == "ser1116b_retire_skill_err"


def test_upgrade_adds_classified_column_and_backfills(mig):
    run = _MigrationRun(_engine_with_table())
    run(mig.upgrade)

    with run.engine.connect() as conn:
        got = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT id, report_type FROM incident_reports ORDER BY id")).all()
        }
        has_col = conn.execute(
            text("SELECT count(*) FROM pragma_table_info('incident_reports') WHERE name='report_type'")
        ).scalar()
    assert has_col == 1
    for rid, _fp, _cmd, _ec, expected in _SEED:
        assert got[rid] == expected, f"{rid} misclassified"


def test_column_is_not_null_with_incident_default(mig):
    """A writer that forgets report_type must land on the conservative side."""
    run = _MigrationRun(_engine_with_table())
    run(mig.upgrade)
    with run.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO incident_reports (id, error_signature, env_fingerprint)"
                " VALUES ('r-new', 'sig-new', '{}')"
            )
        )
        val = conn.execute(text("SELECT report_type FROM incident_reports WHERE id='r-new'")).scalar()
        notnull = conn.execute(
            text("SELECT \"notnull\" FROM pragma_table_info('incident_reports') WHERE name='report_type'")
        ).scalar()
    # 'incident' even though the row is skill-error-shaped: the DEFAULT is not a
    # classifier. Only the backfill (and, from now, the writers) assign skill_error.
    assert val == "incident"
    assert notnull == 1


def test_round_trip_is_walkable(mig):
    """upgrade → downgrade → (index + column gone, data intact)."""
    run = _MigrationRun(_engine_with_table())
    run(mig.upgrade)
    with run.engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM sqlite_master WHERE type='index'"
                    " AND name='ix_incident_reports_report_type'"
                )
            ).scalar()
            == 1
        )

    run(mig.downgrade)
    with run.engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM pragma_table_info('incident_reports') WHERE name='report_type'")
            ).scalar()
            == 0
        )
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM sqlite_master WHERE type='index'"
                    " AND name='ix_incident_reports_report_type'"
                )
            ).scalar()
            == 0
        )
        # the data itself survives a downgrade
        assert conn.execute(text("SELECT count(*) FROM incident_reports")).scalar() == len(_SEED)


def test_upgrade_is_idempotent_safe_on_empty_table(mig):
    """A fresh DB (no rows) must upgrade cleanly and print its 0/0 line."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE incident_reports (id TEXT PRIMARY KEY, command TEXT, exit_code INTEGER, env_fingerprint TEXT)"
            )
        )
    _MigrationRun(engine)(mig.upgrade)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM incident_reports")).scalar() == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, True),
        ({}, True),
        ({"os": "linux"}, False),
        ("{}", True),
        ('{"a":1}', False),
        ("", True),
        ("   ", True),
        ("not json", False),
        ("[]", False),
        (b"{}", True),
        # A JSON scalar is information, not emptiness.
        ("0", False),
        ('"x"', False),
    ],
)
def test_empty_fingerprint_helper(mig, raw, expected):
    assert mig._is_empty_fingerprint(raw) is expected


# ── the Postgres-dialect proof the SQLite test cannot give ────────────────────
#
# Why this file needs its own Postgres test even though
# tests/migrations/test_chain_postgres.py already walks the whole chain on the
# real dialect: a fresh chain DB has ZERO rows in incident_reports, so the
# backfill's UPDATE statement is never executed there. The bug this guards is
# precisely a statement that only fails when it runs —
# sa.update(tbl).values(report_type=...) raises "Unconsumed column names" for any
# column the lightweight sa.table() does not declare, and it raises at COMPILE
# time on the real dialect. With 0 rows it compiles nothing. env_fingerprint is
# also jsonb on Postgres and TEXT on SQLite, so the fingerprint-shape branch is
# only honestly exercised here.


@pytest.fixture()
def pg_dsn():
    """A private empty Postgres DB, or a clean skip when WITH_POSTGRES is unset."""
    if not os.environ.get("WITH_POSTGRES"):
        pytest.skip("Set WITH_POSTGRES=1 + POSTGRES_DSN to run the Postgres backfill proof")
    pytest.importorskip("psycopg2")
    import psycopg2

    admin_dsn = os.environ["POSTGRES_DSN"]
    db_name = f"ser1116c_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db_name};")
    admin.close()
    yield admin_dsn.rsplit("/", 1)[0] + f"/{db_name}"
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    admin.cursor().execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid();",
        (db_name,),
    )
    admin.cursor().execute(f"DROP DATABASE IF EXISTS {db_name};")
    admin.close()


_PG_DDL = """
CREATE TABLE incident_reports (
    id TEXT PRIMARY KEY,
    error_signature TEXT NOT NULL,
    env_fingerprint JSONB NOT NULL,
    agent_fp_anon TEXT,
    occurred_at TIMESTAMPTZ DEFAULT now(),
    command TEXT,
    exit_code INTEGER,
    stack_trace_top TEXT
);
"""


def test_backfill_update_compiles_and_classifies_on_postgres(mig, pg_dsn):
    """The regression that SQLite structurally cannot catch: the backfill's
    UPDATE must compile and run against a jsonb column with real rows present."""
    from sqlalchemy import create_engine

    engine = create_engine(pg_dsn)
    with engine.begin() as conn:
        conn.execute(text(_PG_DDL))
        for rid, fp, cmd, ec, _exp in _SEED:
            conn.execute(
                text(
                    "INSERT INTO incident_reports"
                    " (id, error_signature, env_fingerprint, agent_fp_anon,"
                    "  command, exit_code, stack_trace_top)"
                    " VALUES (:id, :sig, CAST(:fp AS JSONB), 'agent', :cmd, :ec, 'trace')"
                ),
                {"id": rid, "sig": f"sig-{rid}", "fp": fp, "cmd": cmd, "ec": ec},
            )

    # A CompileError here is the historical failure mode, not an assertion failure.
    _MigrationRun(engine)(mig.upgrade)

    with engine.connect() as conn:
        got = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT id, report_type FROM incident_reports ORDER BY id")).all()
        }
        coltype = conn.execute(
            text(
                "SELECT is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name='incident_reports' AND column_name='report_type'"
            )
        ).one()
        has_index = (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes WHERE tablename='incident_reports' "
                    "AND indexname='ix_incident_reports_report_type'"
                )
            ).scalar()
            == 1
        )

    for rid, _fp, _cmd, _ec, expected in _SEED:
        assert got[rid] == expected, f"{rid} misclassified on Postgres"
    assert coltype[0] == "NO", "report_type must be NOT NULL on the production dialect"
    assert "incident" in (coltype[1] or ""), "server_default must carry over to Postgres"
    assert has_index, "the declared index must exist on the production dialect"

    engine.dispose()
