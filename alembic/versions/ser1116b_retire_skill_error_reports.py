"""ser1116b — retire skill_error_reports (fleet sync-report "voice inbox")

WHY THIS TABLE IS BEING DROPPED
-------------------------------
``skill_error_reports`` was created by activate_0701 Phase T as the store for
agent-reported skill errors arriving in ``POST /api/sync-report`` under the
payload key ``skill_errors``. It received **0 rows in its entire existence**
(2026-07-02 → 2026-11-16) and the reason is structural, not incidental:

    The only writer was app/services/sync_report.py reading
    ``payload["skill_errors"]``. The only fleet client that builds that payload,
    scripts/loopskill-collect-reports.py::build_payload(), emits
    ``cycle_ts`` / ``lockfile_state`` / ``loop_runs`` / ``cron_health`` — it has
    never had a ``skill_errors`` key. The single piece of code in the repository
    that ever populated the field was scripts/load_test_sync_report.py, a load
    tester pointed at a disposable DB.

So the field could not fire, while the three surfaces built on top of it kept
advertising that it could: the ``voice_counts.skill_errors`` pending badge on
``GET /api/fleets/{id}/dashboard``, the ``type: "skill_error"`` items in the MCP
tool ``loopskill_voice_inbox_read``, and the FB-phase voice inbox in
``app/voice_routes.py`` (itself already tombstoned 2026-09-22 at 0 requests/30d).
Those readers are removed in the same PR, BEFORE this migration, per the rule
that a table is never dropped while a reader still imports its model.

Skill errors that agents actually report are NOT affected. They are
``IncidentReport`` rows, written by ``POST /api/v1/skill-error``,
``POST /api/feedback/incident`` and the MCP tool
``loopskill_report_skill_error`` — a separate, genuinely live rail.

SAFETY
------
1.0 rows. The migration hard-aborts rather than dropping anything that has data
in it: a non-zero count raises RuntimeError, so an accidental `alembic upgrade
head` against a fork or a self-host install that DID wire a custom emitter gets
a loud failure instead of an irreversible DELETE. Verified empty on production
2026-11-16, and ``pg_dump -t skill_error_reports`` was taken before merge.

DOWNGRADE
---------
Walkable: recreates the table and its four indexes byte-for-byte as
act0701_pt_sync_report.py originally created them (same column types, same
nullability, same ``server_default``s, same index names). Rows are gone — they
were always zero — but the schema is restorable, so `alembic downgrade -1`
followed by `alembic upgrade head` round-trips cleanly and this migration can
be re-applied.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "ser1116b_retire_skill_err"
down_revision = "paywall0925_hits"
branch_labels = None
depends_on = None

TABLE = "skill_error_reports"
INDEXES = (
    "ix_skill_error_reports_created_at",
    "ix_skill_error_reports_slug",
    "ix_skill_error_reports_fleet_id",
    "ix_skill_error_reports_member_id",
)


def _assert_table_empty() -> None:
    """Refuse to drop a table that holds data.

    Production holds 0 rows, so on the real target this is a no-op assertion.
    On any other install (a fork, a self-host that wrote its own emitter against
    the documented payload) it is the difference between a reversible schema
    change and a silent data loss.
    """
    bind = op.get_bind()
    # sa.inspect().has_table() is dialect-portable. The obvious
    # `SELECT 1 FROM information_schema.tables` reads natural here but is
    # Postgres-only and raises OperationalError on the SQLite harness that
    # tests/migrations/ runs the whole chain on — which would take four
    # unrelated migration tests down with it.
    if not sa.inspect(bind).has_table(TABLE):
        # Already absent (idempotent re-apply, or a DB that never ran Phase T).
        return
    n = bind.execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar_one()
    if n:
        raise RuntimeError(
            f"REFUSING to drop {TABLE}: it holds {n} row(s). This table was "
            "retired on the evidence that it never received a row. Export the "
            "rows, delete them, then re-run."
        )


def upgrade() -> None:
    _assert_table_empty()
    for idx in INDEXES:
        op.drop_index(idx, table_name=TABLE)
    op.drop_table(TABLE)


def downgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fleet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("slug", sa.String(255), nullable=False),
        sa.Column("semver", sa.String(32), nullable=True),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("feedback_status", sa.Text(), server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_skill_error_reports_member_id", TABLE, ["member_id"])
    op.create_index("ix_skill_error_reports_fleet_id", TABLE, ["fleet_id"])
    op.create_index("ix_skill_error_reports_slug", TABLE, ["slug"])
    op.create_index("ix_skill_error_reports_created_at", TABLE, ["created_at"])
