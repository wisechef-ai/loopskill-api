"""spotify_2607/0 — federation_hub_skills.owner_handle for ClawHub deep links

Revision ID: sp2607_0_owner_handle
Revises: ah0723_composite_loop_tags
Create Date: 2026-07-26 00:00:00.000000

ClawHub skill pages are owner-scoped (``/<ownerHandle>/skills/<slug>``). We
minted the bare ``/skills/<slug>`` form for all 69,150 ClawHub rows; ClawHub
307s that to ``/skills/skills/<slug>``, a client-rendered soft-404 that still
answers **HTTP 200**. Issue #139 measured the blast radius; PR #140 fixed the
MINT path and centralised URL construction in ``app/services/clawhub_url.py``.

PR #140 could not repair the existing rows, because the owner handle is not in
the Hub snapshot and re-deriving it on every read would mean one upstream call
per row per render. This migration adds the column that lets a one-time
backfill persist the resolution, so the deep link is correct at rest.

Schema change:
  federation_hub_skills.owner_handle  VARCHAR(128) NULL, indexed

Nullable is deliberate and load-bearing:

* A partially-completed backfill is a VALID state. ``clawhub_skill_url()``
  already degrades to the ClawHub browse page when the owner is unknown, so a
  NULL row renders a working (if less precise) link rather than a dead one.
  That makes the backfill safely resumable and idempotent — it can be
  interrupted, re-run, and converge.
* Some slugs genuinely will not resolve upstream (deleted or renamed skills).
  Forcing NOT NULL would mean inventing a handle, and a *guessed* deep link is
  strictly worse than an honest browse-page fallback — it 404s confidently.

VARCHAR(128) matches the ``is_safe_token`` ceiling in ``clawhub_url.py``; any
handle longer than that is rejected as unsafe before it can reach this column,
so the width can never truncate a value we would have accepted.

The index supports the backfill's own progress/resume queries (``WHERE
owner_handle IS NULL``) and the regression check that asserts zero remaining
bare-form URLs.

DOWNGRADE: DROP INDEX + DROP COLUMN. Non-destructive to any other data — the
``origin_url`` values a backfill wrote stay as they are, which is correct: they
are valid URLs regardless of whether we still remember how we derived them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "sp2607_0_owner_handle"
down_revision = "ah0723_composite_loop_tags"
branch_labels = None
depends_on = None

_TABLE = "federation_hub_skills"
_COLUMN = "owner_handle"
_INDEX = "ix_federation_hub_skills_owner_handle"


def upgrade() -> None:
    """Add the nullable, indexed owner_handle column.

    Index creation uses CONCURRENTLY on Postgres to avoid holding a
    write-blocking SHARE lock on ``federation_hub_skills`` for the duration
    of the build. That table gets a full DELETE+re-INSERT from the nightly
    03:00 ``federation_reindex`` cron; a plain (non-concurrent)
    ``CREATE INDEX`` would block that job's writes — and any live serve-path
    reads that touch the table mid-migration — for as long as the index
    build takes. CONCURRENTLY cannot run inside a transaction block, so this
    drops out of Alembic's default transactional DDL via
    ``autocommit_block()`` for just this statement; the column add stays
    transactional.
    """
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}

    # Idempotent: prod has previously accumulated out-of-band merge migrations,
    # so an upgrade must tolerate the column already existing rather than
    # aborting the whole chain.
    if _COLUMN not in existing:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(128), nullable=True))

    existing_indexes = {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}
    if bind.dialect.name == "postgresql":
        # CONCURRENTLY requires autocommit — cannot run inside Alembic's
        # default transaction wrapper (env.py context.begin_transaction()).
        with op.get_context().autocommit_block():
            # A previously-interrupted CREATE INDEX CONCURRENTLY leaves an
            # INVALID index in pg_index (Postgres does not roll it back on
            # failure). SQLAlchemy's get_indexes() does NOT expose
            # indisvalid, so a guard like `if _INDEX not in existing_indexes`
            # would see the INVALID index as "present" and skip this block
            # entirely — leaving a permanently-unusable index in place.
            # Therefore on Postgres we ALWAYS run DROP IF EXISTS (handles
            # absent, VALID, and INVALID cases uniformly) followed by CREATE.
            # DROP/CREATE CONCURRENTLY are both no-ops if the index does not
            # exist, and DROP CONCURRENTLY on a VALID index is the documented
            # way to rebuild it. Cost: one extra CONCURRENTLY build per
            # upgrade invocation, which is cheap on a 70k-row table.
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}"))
            op.execute(sa.text(f"CREATE INDEX CONCURRENTLY {_INDEX} ON {_TABLE} ({_COLUMN})"))
    elif _INDEX not in existing_indexes:
        # SQLite (test suite): CONCURRENTLY is a Postgres-only keyword.
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    """Drop the index and column. Leaves repaired origin_url values intact."""
    bind = op.get_bind()

    existing_indexes = {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}
    if _INDEX in existing_indexes:
        op.drop_index(_INDEX, table_name=_TABLE)

    existing = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN in existing:
        op.drop_column(_TABLE, _COLUMN)
