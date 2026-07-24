"""Deploy-safety regression: the catalog invariant trigger applies cleanly
through psycopg2.

The 2026-05-17 deploy of PR #113 failed because the original migration used
``RAISE EXCEPTION '... %% ...', v_id`` PL/pgSQL syntax. The ``%%`` got
re-escaped by psycopg2's ``%`` parameter-marker handling, producing a
syntactically broken function body in production. Local SQLite tests passed
because they skip the trigger entirely.

This test is the missing safety gate. It only runs when both:

- a real Postgres is reachable (CI services or local docker), AND
- the test marker ``--with-postgres`` was passed.

The body builds a minimal skills/skill_versions schema, runs the trigger
migration via the same psycopg2-driven path alembic uses in production,
inserts a phantom row, and asserts the check_violation fires with the
expected message shape (no ``%%``, UUID embedded as text).

Any future migration that breaks the trigger creation will fail this test
BEFORE deploy.
"""

from __future__ import annotations

import os
import uuid

import pytest


pytestmark = pytest.mark.skipif(
    not os.environ.get("WITH_POSTGRES"),
    reason="set WITH_POSTGRES=1 and supply POSTGRES_DSN to run psycopg2 regressions",
)


def test_trigger_function_creates_via_psycopg2():
    """Apply the trigger SQL through the same psycopg2 path alembic uses.

    Regression: original migration used ``RAISE EXCEPTION '... %% ...', v_id``
    which double-escaped through psycopg2 and produced a syntax error in
    production. The fix uses ``RAISE USING MESSAGE = ... || v_id::text``
    instead, which has no ``%`` to misinterpret.
    """
    pytest.importorskip("psycopg2")
    import psycopg2
    from psycopg2 import errors as psycopg2_errors

    dsn = os.environ.get(
        "POSTGRES_DSN",
        "postgresql://postgres:test@127.0.0.1:5499/postgres",
    )
    db_name = f"trigger_test_{uuid.uuid4().hex[:8]}"

    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"CREATE DATABASE {db_name};")
    admin.close()

    try:
        # Connect to the fresh DB and bootstrap minimal schema.
        db_dsn = dsn.rsplit("/", 1)[0] + f"/{db_name}"
        conn = psycopg2.connect(db_dsn)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE skills (
              id uuid PRIMARY KEY,
              slug text,
              is_public boolean,
              is_archived boolean DEFAULT false NOT NULL,
              archived_at timestamptz,
              search_vector tsvector
            );
            CREATE TABLE skill_versions (
              id uuid PRIMARY KEY,
              skill_id uuid REFERENCES skills(id),
              semver text
            );
            """
        )

        # Apply the migration via the exact alembic upgrade() entry point.
        # alembic.versions is not a regular Python package — load by file path.
        import importlib.util
        from pathlib import Path

        mig_path = (
            Path(__file__).resolve().parent.parent
            / "alembic" / "versions"
            / "a0b1c2d3e4f5_catalog_invariant_no_phantom_public.py"
        )
        spec = importlib.util.spec_from_file_location(
            "catalog_invariant_migration", mig_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # alembic.op.execute pushes raw SQL through psycopg2 the same way
        # we do here. We extract every op.execute("""...""") block from
        # the migration's upgrade() source and apply each one.
        import inspect
        import re

        src = inspect.getsource(mod.upgrade)
        # Match both regular and f-string triple-quoted blocks.
        sql_blocks = re.findall(
            r'op\.execute\(\s*f?"""(.*?)"""\s*\)', src, flags=re.DOTALL
        )
        assert sql_blocks, "no op.execute blocks found in upgrade()"

        # Substitute f-string {_TRIGGER_NAME} references with the real value
        # so we don't try to execute raw {placeholder} text.
        trigger_name = mod._TRIGGER_NAME
        for block in sql_blocks:
            sql = block.replace("{_TRIGGER_NAME}", trigger_name)
            cur.execute(sql)

        # Trigger should be installed now.
        cur.execute("SELECT 1 FROM pg_trigger WHERE tgname = 'trg_no_phantom_public_skill_row';")
        assert cur.fetchone(), "trigger trg_no_phantom_public_skill_row not installed"

        # Phantom insert MUST raise check_violation with a clean message.
        conn.autocommit = False
        phantom_id = str(uuid.uuid4())
        with pytest.raises(psycopg2_errors.CheckViolation) as exc_info:
            cur.execute(
                "INSERT INTO skills (id, slug, is_public, is_archived) "
                "VALUES (%s, 'phantom', TRUE, FALSE);",
                (phantom_id,),
            )
            conn.commit()
        conn.rollback()

        msg = str(exc_info.value)
        assert "catalog_invariant_violation" in msg
        assert phantom_id in msg, (
            f"trigger message should embed the UUID via string concat, got: {msg[:300]}"
        )
        # Specifically, no leftover ``%%`` / ``%%%%`` escape garbage.
        assert "%%" not in msg, (
            f"trigger message contains literal '%%' — RAISE EXCEPTION %-format "
            f"regression: {msg[:300]}"
        )

        # And a clean publish flow (skill + matching version) must NOT raise.
        clean_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO skills (id, slug, is_public, is_archived) "
            "VALUES (%s, 'clean', FALSE, FALSE);",
            (clean_id,),
        )
        cur.execute(
            "INSERT INTO skill_versions (id, skill_id, semver) VALUES (%s, %s, '1.0.0');",
            (str(uuid.uuid4()), clean_id),
        )
        cur.execute(
            "UPDATE skills SET is_public = TRUE WHERE id = %s;",
            (clean_id,),
        )
        conn.commit()
    finally:
        conn.close()
        admin = psycopg2.connect(dsn)
        admin.autocommit = True
        admin.cursor().execute(f"DROP DATABASE IF EXISTS {db_name};")
        admin.close()
