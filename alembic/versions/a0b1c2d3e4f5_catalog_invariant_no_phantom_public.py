"""Catalog invariant — partial unique index preventing phantom public rows

Issue #109: ensure no future row can land in the state
``is_public=true AND is_archived=false AND no rows in skill_versions``.

Implementation: a partial unique index keyed on ``id`` that ONLY indexes rows
matching the *forbidden* shape. Because the underlying column ``skills.id`` is
already a primary key (so its values are guaranteed unique), the partial
unique index can NEVER reject an UPDATE/INSERT on its own — but the *invariant*
we actually care about is enforced by a CHECK + trigger combination below
that uses the partial index for fast filtered scans.

Postgres-specific. SQLite (used in the test fixture) silently skips the
trigger; the Python-level invariant test in
``tests/test_catalog_invariant_no_phantom_public.py`` is the test-environment
gate.

Revision ID: a0b1c2d3e4f5
Revises: f00d1109cafe
Create Date: 2026-05-17 21:05:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, None] = "f00d1109cafe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TRIGGER_NAME = "trg_no_phantom_public_skill_row"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # Tests use SQLite — no PL/pgSQL trigger support. The Python-level
        # invariant test enforces the same shape on SQLite.
        return

    # Function: raise EXCEPTION if a row is public + not archived + has no
    # published version. Runs on INSERT / UPDATE of skills, and on DELETE of
    # skill_versions (which is where a "made-it-orphan" race would land).
    #
    # NOTE: do NOT use ``%`` placeholders inside PL/pgSQL RAISE EXCEPTION when
    # the SQL passes through psycopg2 — the driver re-interprets ``%`` as a
    # parameter marker and double-escapes literal percent signs. Use ``RAISE
    # USING MESSAGE = '...' || v_id::text`` instead so the message is built
    # with string concatenation; no ``%`` in the SQL, no escape ambiguity.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION catalog_no_phantom_public_skill()
        RETURNS trigger AS $$
        DECLARE
            v_count int;
            v_id uuid;
        BEGIN
            -- Pick the row to check based on which table fired the trigger.
            IF TG_TABLE_NAME = 'skills' THEN
                IF NEW.is_public = FALSE OR NEW.is_archived = TRUE THEN
                    RETURN NEW;
                END IF;
                v_id := NEW.id;
            ELSIF TG_TABLE_NAME = 'skill_versions' THEN
                v_id := OLD.skill_id;
                -- If the parent row is already private/archived, nothing to check.
                PERFORM 1 FROM skills
                WHERE id = v_id AND is_public = TRUE AND is_archived = FALSE;
                IF NOT FOUND THEN
                    RETURN OLD;
                END IF;
            ELSE
                RETURN NULL;
            END IF;

            SELECT COUNT(*) INTO v_count
            FROM skill_versions
            WHERE skill_id = v_id;

            IF v_count = 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'check_violation',
                    MESSAGE = 'catalog_invariant_violation: skill ' || v_id::text
                              || ' is_public=true and is_archived=false but has '
                              || 'no published versions. Either publish a version '
                              || 'or set is_archived=true.';
            END IF;

            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    # Trigger on skills (INSERT, UPDATE of is_public/is_archived).
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON skills;
        CREATE CONSTRAINT TRIGGER {_TRIGGER_NAME}
        AFTER INSERT OR UPDATE OF is_public, is_archived ON skills
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION catalog_no_phantom_public_skill();
        """
    )

    # Trigger on skill_versions DELETE — if a row's last version is deleted,
    # the parent had better be private or archived.
    op.execute(
        f"""
        DROP TRIGGER IF EXISTS {_TRIGGER_NAME}_versions ON skill_versions;
        CREATE CONSTRAINT TRIGGER {_TRIGGER_NAME}_versions
        AFTER DELETE ON skill_versions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION catalog_no_phantom_public_skill();
        """
    )

    # Helpful partial index for the periodic auditor + the test invariant
    # query (issue #110 backfill cron uses this too).
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_skills_phantom_audit
        ON skills (id)
        WHERE is_public = TRUE AND is_archived = FALSE;
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON skills;")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME}_versions ON skill_versions;")
    op.execute("DROP FUNCTION IF EXISTS catalog_no_phantom_public_skill();")
    op.execute("DROP INDEX IF EXISTS ix_skills_phantom_audit;")
