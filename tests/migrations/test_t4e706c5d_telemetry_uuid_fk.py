"""Test for t4e706c5d — telemetry_events FK columns widened varchar → uuid.

a7f7db696591 created ``skill_id`` / ``install_event_id`` as unconditional
``sa.String(36)`` while the model declares ``UUID(as_uuid=True)`` + FK, so on
the production dialect the advertised relationship has NEVER been traversable:

    ERROR:  operator does not exist: uuid = character varying
    LINE 6: left join install_events ie on ie.id = t.install_event_id

Measured on prod 2026-11-17: both columns NULL in 5719/5719 rows, no FK
constraint exists (PK only). The migration this file proves refuses to run on
data the cast/FK would reject (open-core self-hosters may hold such data) and
otherwise widens both columns to native uuid + creates the two FK constraints
the model has advertised all along.

Why this needs its own seeded Postgres proof even though
``tests/migrations/test_chain_postgres.py`` walks the whole chain: a fresh chain
DB has ZERO telemetry rows, so neither the refusal guards nor the ``USING``
cast ever executes there. This file seeds one row per shape bucket (valid uuid /
NULL / invalid string / orphan) and drives ``upgrade()`` through
``Operations.context`` against a real Postgres — and embeds the RED side of the
proof: the card's exact join is asserted to FAIL with ``operator does not exist``
on the pre-migration schema before it is asserted to work after.
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

MOD_NAME = "t4e706c5d_telemetry_uuid_fk"

_FK_SKILL = "fk_telemetry_events_skill_id"
_FK_INSTALL = "fk_telemetry_events_install_event_id"


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


# ── SQLite leg: the migration must be a clean no-op there ────────────────────


def test_migration_metadata(mig) -> None:
    assert mig.revision == "t4e706c5d_telemetry_uuid_fk"
    # Pinned to the single head at authoring. If this fails because another
    # migration merged first, RE-PIN — do not add a second head.
    assert mig.down_revision == "ser1116c_incident_report_type"


def test_sqlite_leg_is_a_clean_noop(mig) -> None:
    """SQLite has no native UUID; the migration must leave it untouched.

    Mirrors the fc0706b_snap_uuid carve-out. A regression here (e.g. someone
    drops the dialect check) fails the whole SQLite migration chain.
    """
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE telemetry_events (id TEXT PRIMARY KEY, event_type TEXT,"
                " skill_id VARCHAR(36), install_event_id VARCHAR(36))"
            )
        )
        conn.execute(
            text(
                "INSERT INTO telemetry_events (id, event_type, skill_id, install_event_id)"
                " VALUES ('t1', 'install', NULL, NULL)"
            )
        )
    _MigrationRun(engine)(mig.upgrade)
    _MigrationRun(engine)(mig.downgrade)
    with engine.connect() as conn:
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(telemetry_events)")).all()]
        n = conn.execute(text("SELECT count(*) FROM telemetry_events")).scalar()
    assert {"skill_id", "install_event_id"} <= set(cols)
    assert n == 1


# ── the Postgres-dialect proof the SQLite test cannot give ───────────────────


@pytest.fixture()
def pg_dsn():
    """A private empty Postgres DB, or a clean skip when WITH_POSTGRES is unset."""
    if not os.environ.get("WITH_POSTGRES"):
        pytest.skip("Set WITH_POSTGRES=1 + POSTGRES_DSN to run the Postgres uuid-widening proof")
    pytest.importorskip("psycopg2")
    import psycopg2

    admin_dsn = os.environ["POSTGRES_DSN"]
    db_name = f"t4e706c5d_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db_name};")  # noqa: S608 - generated uuid suffix
    admin.close()
    yield admin_dsn.rsplit("/", 1)[0] + f"/{db_name}"
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    admin.cursor().execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        "WHERE datname = %s AND pid <> pg_backend_pid();",
        (db_name,),
    )
    admin.cursor().execute(f"DROP DATABASE IF EXISTS {db_name};")  # noqa: S608 - generated uuid suffix
    admin.close()


# Pre-migration state, shaped exactly like prod measured 2026-11-17: uuid PKs,
# varchar(36) FK columns, no FK constraints.
_PG_PRE_STATE = """
CREATE TABLE skills (id uuid PRIMARY KEY);
CREATE TABLE install_events (id uuid PRIMARY KEY);
CREATE TABLE telemetry_events (
    id uuid PRIMARY KEY,
    event_type varchar(128) NOT NULL,
    skill_slug varchar(255),
    skill_id varchar(36),
    install_event_id varchar(36)
);
"""

# The exact join shape from the card (t_61421779's prod measurement attempt).
_THE_JOIN = (
    "SELECT t.id, ie.id FROM telemetry_events t LEFT JOIN install_events ie ON ie.id = t.install_event_id"
)


def _seed_clean(cur) -> tuple[str, str, str, str]:
    """One valid-uuid-linked row + one all-NULL row, over real parent rows.

    Params are strings (the t61421779 harness convention): PG casts text →
    uuid on insert, and we never depend on the driver's UUID adapter being
    registered — this venv's psycopg2 only adapts UUID objects when another
    module has registered the adapter first.
    """
    skill = str(uuid.uuid4())
    install = str(uuid.uuid4())
    linked, nulled = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO skills (id) VALUES (%s);", (skill,))
    cur.execute("INSERT INTO install_events (id) VALUES (%s);", (install,))
    cur.execute(
        "INSERT INTO telemetry_events (id, event_type, skill_slug, skill_id, install_event_id)"
        " VALUES (%s, 'install', 'linked-skill', %s, %s);",
        (linked, skill, install),
    )
    cur.execute(
        "INSERT INTO telemetry_events (id, event_type, skill_slug, skill_id, install_event_id)"
        " VALUES (%s, 'install', NULL, NULL, NULL);",
        (nulled,),
    )
    return linked, nulled, skill, install


def _col_type(conn, col: str) -> str:
    return conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'telemetry_events' AND column_name = :c"
        ),
        {"c": col},
    ).scalar_one()


def _fk_names(conn) -> list[str]:
    return sorted(
        r[0]
        for r in conn.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'telemetry_events'::regclass AND contype = 'f'"
            )
        ).all()
    )


def test_uuid_join_executes_after_upgrade_on_postgres(mig, pg_dsn):
    """RED→GREEN on the real dialect: the card's join fails before, works after."""
    import psycopg2

    engine = create_engine(pg_dsn)
    conn = psycopg2.connect(pg_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(_PG_PRE_STATE)
    linked, _nulled, _skill, install = _seed_clean(cur)
    conn.close()

    # RED: on the pre-migration schema this is the production error, verbatim.
    # Raw psycopg2 (not engine.execute) so the raised type is exactly the
    # production one — SQLAlchemy wraps it as ProgrammingError otherwise.
    probe = psycopg2.connect(pg_dsn)
    probe.autocommit = True
    with pytest.raises(psycopg2.errors.UndefinedFunction, match="operator does not exist"):
        probe.cursor().execute(_THE_JOIN)
    probe.close()

    # GREEN: widen + FK, then the same join must execute and match.
    _MigrationRun(engine)(mig.upgrade)
    with engine.connect() as conn:
        rows = {
            str(r[0]): (str(r[1]) if r[1] is not None else None) for r in conn.execute(text(_THE_JOIN)).all()
        }
        skill_rows = (
            conn.execute(text("SELECT t.id FROM telemetry_events t JOIN skills s ON s.id = t.skill_id"))
            .scalars()
            .all()
        )
    assert rows[linked] == install, "linked row must join its install_event"
    assert None in rows.values(), "all-NULL row must survive as an unmatched LEFT JOIN"
    assert len(skill_rows) == 1, "skill_id must be joinable to skills too"
    with engine.connect() as conn:
        assert _col_type(conn, "install_event_id") == "uuid"
        assert _col_type(conn, "skill_id") == "uuid"
        assert _fk_names(conn) == [_FK_INSTALL, _FK_SKILL]

    engine.dispose()


def test_upgrade_refuses_on_non_uuid_garbage(mig, pg_dsn):
    """The cast would hard-fail on garbage; the migration must refuse first."""
    import psycopg2

    engine = create_engine(pg_dsn)
    conn = psycopg2.connect(pg_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(_PG_PRE_STATE)
    garbage_row = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO telemetry_events (id, event_type, install_event_id)"
        " VALUES (%s, 'install', 'not-a-uuid');",
        (garbage_row,),
    )
    conn.close()

    with pytest.raises(RuntimeError, match="not.*valid UUID"):
        _MigrationRun(engine)(mig.upgrade)

    # Refused BEFORE any DDL: column still varchar, row intact.
    with engine.connect() as conn:
        assert _col_type(conn, "install_event_id") == "character varying"
        val = conn.execute(
            text("SELECT install_event_id FROM telemetry_events WHERE id = :i"),
            {"i": garbage_row},
        ).scalar_one()
    assert val == "not-a-uuid"
    engine.dispose()


def test_upgrade_refuses_on_orphaned_uuid(mig, pg_dsn):
    """FK validation would fail mid-DDL on orphans; refuse with remediation."""
    import psycopg2

    engine = create_engine(pg_dsn)
    conn = psycopg2.connect(pg_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(_PG_PRE_STATE)
    orphan_row = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO telemetry_events (id, event_type, install_event_id) VALUES (%s, 'install', %s);",
        (orphan_row, str(uuid.uuid4())),
    )  # valid uuid text, but no install_events row carries it
    conn.close()

    with pytest.raises(RuntimeError, match="no matching"):
        _MigrationRun(engine)(mig.upgrade)

    with engine.connect() as conn:
        assert _col_type(conn, "install_event_id") == "character varying"
        assert _fk_names(conn) == []
    engine.dispose()


def test_round_trip_walks_both_ways_on_postgres(mig, pg_dsn):
    """upgrade → downgrade → upgrade keeps data and ends typed + constrained."""
    import psycopg2

    engine = create_engine(pg_dsn)
    conn = psycopg2.connect(pg_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(_PG_PRE_STATE)
    linked, _nulled, _skill, install = _seed_clean(cur)
    conn.close()

    _MigrationRun(engine)(mig.upgrade)
    _MigrationRun(engine)(mig.downgrade)
    with engine.connect() as conn:
        assert _col_type(conn, "install_event_id") == "character varying"
        assert _col_type(conn, "skill_id") == "character varying"
        assert _fk_names(conn) == []
        val = conn.execute(
            text("SELECT install_event_id FROM telemetry_events WHERE id = :i"),
            {"i": linked},
        ).scalar_one()
    assert val == install, "downgrade must not lose data"

    _MigrationRun(engine)(mig.upgrade)
    with engine.connect() as conn:
        assert _col_type(conn, "install_event_id") == "uuid"
        assert _fk_names(conn) == [_FK_INSTALL, _FK_SKILL]
        assert conn.execute(text("SELECT count(*) FROM telemetry_events")).scalar_one() == 2
    engine.dispose()
