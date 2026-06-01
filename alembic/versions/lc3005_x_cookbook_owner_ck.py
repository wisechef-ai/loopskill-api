"""loopclose_3005 Phase X — cookbook ownership invariant + orphan cleanup

Revision ID: lc3005_x_cookbook_owner_ck
Revises: lc3005_d_revert_founding
Create Date: 2026-06-01 20:00:00.000000

Closes the cookbook-ownership class of bug for good (Adam, 2026-06-01, Q1:
"fix the root cause for any user"). Two layers, in strict order:

  1. DATA STEP (first): delete every non-base, owner-less "orphan" cookbook.
     These are May test debris from the recipes_recipify NULL-owner bug — no
     recoverable owner, invisible to every user. The is_base=true system
     catalog ("WiseChef Recipes Catalog", 81 skills) is legitimately
     owner-less and is LEFT INTACT.

  2. CONSTRAINT STEP (after cleanup): add a CHECK invariant
     (is_base = true OR cookbook_owner IS NOT NULL) so the class can never
     recur — any future code path that forgets ownership now fails at the DB,
     not silently. The order matters: the constraint would reject the existing
     NULL-owner rows if added before the cleanup.

Postgres-only DDL (named CHECK constraint). Per
alembic-postgres-only-sql-discipline the SQLite test fixture cannot enforce
the CHECK — the real proof is tests/test_loopclose_3005_x_migration_psycopg2.py
run against a throwaway Postgres. The migration branches on dialect so SQLite
upgrades remain a clean no-op for the constraint.

DOWNGRADE drops the constraint only (deleted orphans are not resurrected —
they were unrecoverable test debris).
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "lc3005_x_cookbook_owner_ck"
down_revision = "lc3005_d_revert_founding"
branch_labels = None
depends_on = None

_CK_NAME = "ck_cookbooks_owner_required"


def upgrade() -> None:
    """Delete owner-less non-base orphans, then enforce the ownership invariant."""
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # ── Step 1 — DATA: remove the orphan test debris (non-base, owner-less). ──
    # Must run BEFORE the CHECK constraint or the constraint fails on these rows.
    # is_base=true catalogs are legitimately owner-less and are preserved.
    op.execute(
        "DELETE FROM cookbooks WHERE is_base = false AND cookbook_owner IS NULL"
    )

    # ── Step 2 — CONSTRAINT: a non-base cookbook must always have an owner. ───
    if is_postgres:
        op.create_check_constraint(
            _CK_NAME,
            "cookbooks",
            "is_base = true OR cookbook_owner IS NOT NULL",
        )


def downgrade() -> None:
    """Drop the ownership CHECK (deleted orphans are not resurrected)."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(_CK_NAME, "cookbooks", type_="check")
