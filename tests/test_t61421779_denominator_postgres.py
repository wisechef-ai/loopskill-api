"""t_61421779 — the new denominator must execute on Postgres, not just compile.

``_installs_7d`` replaced a single-table count on ``telemetry_events`` with a count
over ``install_events`` carrying TWO new outer joins (``api_keys`` on
``api_key_id``, ``users`` on ``api_keys.user_id``) plus three predicates, one of
which SQLAlchemy renders as ``IS false``. Every one of those touches a type that
SQLite does not model the way production does — verified against prod
2026-11-16 via information_schema:

    install_events.id / skill_id / api_key_id   uuid
    install_events.created_at                   timestamp with time zone
    install_events.is_probe                     boolean NOT NULL
    api_keys.id / user_id, users.id             uuid
    api_keys.is_test, users.is_agent            boolean NOT NULL

On the SQLite harness those same ids are 32-char hex TEXT and ``IS false`` becomes
``IS 0``, so a whole class of breakage — a uuid-vs-varchar join the way
``telemetry_events.install_event_id`` already has, a timestamptz comparison that
errors or silently mis-bounds — is invisible to ``db_session`` and only ever
surfaces in production. That is the same failure shape as the
``catalog invariant trigger`` psycopg2 regression, and it is why this file exists
next to the SQLite tests rather than instead of them.

Runs only under ``WITH_POSTGRES=1`` + ``POSTGRES_DSN``, i.e. exactly the CI
``migrations-postgres`` environment.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("WITH_POSTGRES"),
    reason="set WITH_POSTGRES=1 and supply POSTGRES_DSN to run the Postgres-dialect denominator regression",
)

_SERVER_IP = "203.0.113.9"
_DOGFOOD_IP = "198.51.100.7"
_EXTERNAL_IP = "54.83.240.58"

# Column types copied from the live prod information_schema, not from the ORM —
# the point of the test is that the ORM matches reality on Postgres.
_SCHEMA = """
CREATE TABLE users (
    id uuid PRIMARY KEY,
    is_agent boolean NOT NULL DEFAULT false
);
CREATE TABLE api_keys (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    is_test boolean NOT NULL DEFAULT false
);
CREATE TABLE install_events (
    id uuid PRIMARY KEY,
    skill_id uuid NOT NULL,
    skill_slug character varying(255),
    api_key_id uuid REFERENCES api_keys(id),
    status character varying(32) NOT NULL DEFAULT 'ok',
    attribution character varying(16) NOT NULL DEFAULT 'attributed',
    is_probe boolean NOT NULL DEFAULT false,
    client_ip character varying(64),
    created_at timestamp with time zone
);
CREATE TABLE incident_reports (
    id uuid PRIMARY KEY,
    report_type character varying(32) NOT NULL DEFAULT 'incident',
    occurred_at timestamp with time zone
);
"""


def _seed(conn, *, now, human_id, test_key_id, agent_key_id):
    """One row per shape bucket the denominator is supposed to distinguish."""

    def ins(table, cols, vals):
        ph = ", ".join(["%s"] * len(vals))
        conn.cursor().execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph})",  # noqa: S608 - fixed DDL above
            vals,
        )

    skill = str(uuid.uuid4())
    # 5 rows that must count.
    for _ in range(3):
        ins(
            "install_events",
            ["id", "skill_id", "skill_slug", "api_key_id", "status", "is_probe", "client_ip", "created_at"],
            [str(uuid.uuid4()), skill, "pg-shape", None, "ok", False, None, now],
        )
    for _ in range(2):
        ins(
            "install_events",
            ["id", "skill_id", "skill_slug", "api_key_id", "status", "is_probe", "client_ip", "created_at"],
            [str(uuid.uuid4()), skill, "pg-shape", human_id, "ok", False, _EXTERNAL_IP, now],
        )
    # 5 rows that must NOT.
    excluded = [
        dict(status="ok", is_probe=True, api_key_id=None, client_ip=None, when=now),  # fleet probe
        dict(status="rolled_back", is_probe=False, api_key_id=None, client_ip=None, when=now),
        dict(status="partial", is_probe=False, api_key_id=None, client_ip=None, when=now),
        dict(status="ok", is_probe=False, api_key_id=str(test_key_id), client_ip=None, when=now),
        dict(status="ok", is_probe=False, api_key_id=str(agent_key_id), client_ip=None, when=now),
        dict(status="ok", is_probe=False, api_key_id=None, client_ip=_SERVER_IP, when=now),
        dict(status="ok", is_probe=False, api_key_id=None, client_ip=_DOGFOOD_IP, when=now),
        dict(status="ok", is_probe=False, api_key_id=None, client_ip=None, when=now - timedelta(days=8)),
    ]
    for e in excluded:
        ins(
            "install_events",
            ["id", "skill_id", "skill_slug", "api_key_id", "status", "is_probe", "client_ip", "created_at"],
            [
                str(uuid.uuid4()),
                skill,
                "pg-shape",
                e["api_key_id"],
                e["status"],
                e["is_probe"],
                e["client_ip"],
                e["when"],
            ],
        )
    # Two skill errors + one crash, so the numerator half executes too.
    for _ in range(2):
        ins("incident_reports", ["id", "report_type", "occurred_at"], [str(uuid.uuid4()), "skill_error", now])
    ins("incident_reports", ["id", "report_type", "occurred_at"], [str(uuid.uuid4()), "incident", now])
    return skill


def test_denominator_query_executes_and_filters_on_postgres(monkeypatch):
    """The whole denominator runs on real Postgres and filters exactly right.

    Asserts a NUMBER, not merely "no exception" — a query that silently returns 0
    on Postgres because a uuid join failed to match is precisely the bug a
    smoke-test would miss, and 0 is also the value this card exists to eliminate.
    """
    pytest.importorskip("psycopg2")
    import psycopg2
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from app.transparency_routes import _compute_skill_error_rate_7d, _installs_7d

    monkeypatch.setattr("app.config.settings.SERVER_PUBLIC_IP", _SERVER_IP)
    monkeypatch.setattr("app.config.settings.KNOWN_INTERNAL_IPS", [_DOGFOOD_IP])

    dsn = os.environ["POSTGRES_DSN"]
    db_name = f"t6142_denom_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    admin.cursor().execute(f"CREATE DATABASE {db_name};")  # noqa: S608 - generated uuid suffix
    admin.close()
    db_dsn = dsn.rsplit("/", 1)[0] + f"/{db_name}"

    now = datetime.now(UTC)
    engine = None
    try:
        conn = psycopg2.connect(db_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(_SCHEMA)

        human_user, agent_user = str(uuid.uuid4()), str(uuid.uuid4())
        cur.execute("INSERT INTO users (id, is_agent) VALUES (%s, false);", (human_user,))
        cur.execute("INSERT INTO users (id, is_agent) VALUES (%s, true);", (agent_user,))
        human_key = str(uuid.uuid4())
        test_key = str(uuid.uuid4())
        agent_key = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO api_keys (id, user_id, is_test) VALUES (%s, %s, false);", (human_key, human_user)
        )
        cur.execute(
            "INSERT INTO api_keys (id, user_id, is_test) VALUES (%s, %s, true);", (test_key, human_user)
        )
        cur.execute(
            "INSERT INTO api_keys (id, user_id, is_test) VALUES (%s, %s, false);", (agent_key, agent_user)
        )

        _seed(conn, now=now, human_id=human_key, test_key_id=test_key, agent_key_id=agent_key)
        # Sanity: the rows are really there, so a 0 below can only mean the query
        # mis-filtered rather than the fixture being empty.
        cur.execute("SELECT count(*) FROM install_events;")
        seeded = cur.fetchone()
        assert seeded is not None and seeded[0] == 13
        conn.close()

        engine = create_engine(db_dsn)
        with Session(engine) as session:
            assert _installs_7d(session) == 5, (
                "Postgres filtered the denominator differently from SQLite — "
                "check the uuid joins and the IS false / timestamptz predicates"
            )
            # 2 skill errors over the 5 real installs. The crash row must stay out.
            assert _compute_skill_error_rate_7d(session) == pytest.approx(0.4)
    finally:
        # Dispose BEFORE the drop or PostgreSQL refuses it (ObjectInUse) and the
        # throwaway database leaks on every CI run that takes this branch.
        if engine is not None:
            engine.dispose()
        admin = psycopg2.connect(dsn)
        admin.autocommit = True
        admin_cur = admin.cursor()
        admin_cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid();",
            (db_name,),
        )
        admin_cur.execute(f"DROP DATABASE IF EXISTS {db_name};")  # noqa: S608 - generated uuid suffix
        admin.close()
