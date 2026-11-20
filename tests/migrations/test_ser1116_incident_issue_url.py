"""ser1116 must be the revision that puts issue_url / feedback_status on
incident_reports — proven on the production dialect, not SQLite.

WHY THIS FILE EXISTS
--------------------
`test_chain_postgres.py::test_model_columns_present_after_full_upgrade` sounds
like it would catch a forgotten migration, but it only iterates the five
bootstrapped tables (User, APIKey, Creator, CreatorPayout, Referral). A column
added to any other model with no migration sails straight through it, and the
SQLite suite cannot see Postgres DDL at all. So `incident_reports.issue_url` had
no parity gate covering it.

This test closes that hole and pins causality in both directions:
  * at ser1116's parent revision the columns are ABSENT,
  * after `upgrade head` they are PRESENT with the right types/nullability,
  * `downgrade -1` removes them again (the migration is reversible — required,
    because this migration ships to production unattended).

Run it with the one-shot runner:
    bash scripts/test-migrations-against-postgres.sh -k ser1116
"""

from __future__ import annotations

import os

import pytest

# Reuse the exact fixture/helper plumbing from the chain test rather than
# re-implementing Postgres provisioning — same DSN contract, same teardown.
from tests.migrations.test_chain_postgres import (
    BASELINE_REV,
    _run_alembic,
    _seed_baseline_schema,
    fresh_postgres_db,  # noqa: F401  (pytest fixture imported for reuse)
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("WITH_POSTGRES"),
    reason=(
        "Set WITH_POSTGRES=1 and POSTGRES_DSN to run migration tests against real "
        "Postgres. Use scripts/test-migrations-against-postgres.sh for a one-shot local run."
    ),
)

REVISION = "ser1116_incident_issue_url"
PARENT_REV = "t4e706c5d_telemetry_uuid_fk"
TABLE = "incident_reports"


def _live_columns(cur, table: str) -> dict[str, dict[str, object]]:
    cur.execute(
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name = %s;",
        (table,),
    )
    return {r[0]: {"data_type": r[1], "is_nullable": r[2], "column_default": r[3]} for r in cur.fetchall()}


def _upgrade_to(dsn: str, rev: str) -> None:
    """Seed the prod baseline, stamp it, then walk the chain to `rev`."""
    _seed_baseline_schema(dsn)
    assert _run_alembic(["stamp", BASELINE_REV], dsn).returncode == 0
    res = _run_alembic(["upgrade", rev], dsn)
    assert res.returncode == 0, f"upgrade to {rev} failed:\n{res.stdout}\n{res.stderr}"


def test_ser1116_is_head_and_its_parent_is_correct():
    """Guards against the new revision being orphaned or mis-linked, which would
    make it never run in production while every local test still passes."""
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    res = subprocess.run(
        [sys.executable, "-m", "alembic", "heads", "--verbose"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert REVISION in res.stdout, f"ser1116 is not an alembic head:\n{res.stdout}"
    assert "(head)" in res.stdout


def test_columns_absent_at_parent_and_present_at_head(fresh_postgres_db):  # noqa: F811
    """The migration must be the thing that adds them — nothing else."""
    import psycopg2

    _upgrade_to(fresh_postgres_db, PARENT_REV)

    conn = psycopg2.connect(fresh_postgres_db)
    conn.autocommit = True
    cur = conn.cursor()

    before = _live_columns(cur, TABLE)
    assert "issue_url" not in before, "issue_url existed before ser1116 — the gate proves nothing"
    assert "feedback_status" not in before, "feedback_status existed before ser1116 — the gate proves nothing"
    conn.close()

    up = _run_alembic(["upgrade", "head"], fresh_postgres_db)
    assert up.returncode == 0, f"upgrade head failed:\n{up.stdout}\n{up.stderr}"

    conn = psycopg2.connect(fresh_postgres_db)
    conn.autocommit = True
    cur = conn.cursor()

    after = _live_columns(cur, TABLE)
    assert {
        "issue_url",
        "feedback_status",
    } <= after.keys(), f"ser1116 did not create the write-back columns; {TABLE} has {sorted(after)}"
    # issue_url must accept NULL: /api/feedback/incident files no GitHub issue.
    assert after["issue_url"]["is_nullable"] == "YES", "issue_url must be nullable"
    assert after["issue_url"]["data_type"] == "text"
    # feedback_status must have a DB-level default so pre-existing rows are valid.
    assert (
        after["feedback_status"]["column_default"] is not None
    ), "feedback_status needs a server default; existing rows were backfilled by the migration"
    conn.close()


def test_existing_rows_are_backfilled_not_null(fresh_postgres_db):  # noqa: F811
    """The migration backfills feedback_status='pending'.

    Without it, every incident row written before the deploy would read NULL and
    the public status endpoint would report a status the schema never intended.
    """
    import psycopg2

    _upgrade_to(fresh_postgres_db, PARENT_REV)

    conn = psycopg2.connect(fresh_postgres_db)
    conn.autocommit = True
    cur = conn.cursor()
    # incident_reports.skill_id / env_fingerprint / agent_fp_anon are NOT NULL, so
    # the row that straddles the deploy boundary has to be a fully valid one.
    # is_archived=true is required: the catalog_no_phantom_public_skill trigger
    # rejects a public, non-archived skill with no published versions.
    skill_id = "00000000-0000-0000-0000-000000000f45"
    report_id = "00000000-0000-0000-0000-000000000f4e"
    cur.execute(
        "INSERT INTO skills (id, slug, title, is_archived) VALUES (%s, %s, %s, true)",
        (skill_id, "f4-backfill-probe", "f4 backfill probe"),
    )
    cur.execute(
        # NB: implicit string concatenation — only the FIRST literal is an f-string,
        # so the '{}' JSON literal below is literal text, not an empty field.
        f"INSERT INTO {TABLE} (id, skill_id, error_signature, env_fingerprint, agent_fp_anon, occurred_at) "
        "VALUES (%s, %s, %s, '{}'::json, %s, now())",
        (report_id, skill_id, "ab" * 32, "agent-f4-backfill"),
    )
    conn.close()

    up = _run_alembic(["upgrade", "head"], fresh_postgres_db)
    assert up.returncode == 0, f"upgrade head failed with pre-existing rows:\n{up.stdout}\n{up.stderr}"

    conn = psycopg2.connect(fresh_postgres_db)
    cur = conn.cursor()
    cur.execute(f"SELECT feedback_status, issue_url FROM {TABLE} WHERE id = %s", (report_id,))
    row = cur.fetchone()
    conn.close()

    assert row is not None, "pre-existing row was lost by the migration"
    assert row[0] == "pending", f"pre-existing row got feedback_status={row[0]!r}, expected 'pending'"
    assert row[1] is None, "issue_url must stay NULL for a row that never had an issue"


def test_downgrade_is_reversible(fresh_postgres_db):  # noqa: F811
    """This migration ships to production unattended; a one-way door is not acceptable."""
    import psycopg2

    _upgrade_to(fresh_postgres_db, "head")

    down = _run_alembic(["downgrade", "-1"], fresh_postgres_db)
    assert down.returncode == 0, f"downgrade failed:\n{down.stdout}\n{down.stderr}"

    conn = psycopg2.connect(fresh_postgres_db)
    conn.autocommit = True
    cur = conn.cursor()
    after = _live_columns(cur, TABLE)
    conn.close()

    assert "issue_url" not in after, "downgrade left issue_url behind"
    assert "feedback_status" not in after, "downgrade left feedback_status behind"
